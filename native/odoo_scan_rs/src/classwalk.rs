//! Port of `ClassFileIndex._index` of `odoo_typing.registry.class_index` (one record):
//! the kept record parsed (`_parse`), the addons walked with their stamps (`_walk`,
//! `_file`, `_entries`, `matches`) and the record of what was read (`_encode`).
use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PyString, PyTuple};
use rayon::prelude::*;
use std::collections::{BTreeSet, HashMap};
use std::os::unix::fs::MetadataExt;

pub type Stamp = Option<(i64, i64)>;
const UNREADABLE: (i64, i64) = (-1, -1);
const NO_MODEL_DIRS: [&str; 7] = ["static", "i18n", "node_modules", "__pycache__", "tests", "migrations", "upgrades"];

#[derive(Clone, PartialEq)]
struct File {
    stamp: Stamp,
    models: BTreeSet<String>,
}

#[derive(PartialEq)]
struct Directory {
    stamp: Stamp,
    files: IndexMap<String, File>,
    subdirs: Vec<String>,
}

/// Where Python must walk instead (a name that is not UTF-8).
pub struct Fallback;

/// What `os.stat` gives the walk: the stamp and, for a directory, its identity.
type Status = Option<((i64, i64), (u64, u64))>;

fn stat(path: &str) -> Status {
    let m = std::fs::metadata(path).ok()?;
    Some(((m.mtime() * 1_000_000_000 + m.mtime_nsec(), m.size() as i64), (m.dev(), m.ino())))
}

extern "C" {
    fn access(path: *const std::ffi::c_char, mode: std::ffi::c_int) -> std::ffi::c_int;
}

/// `stamps._unreadable`: a file that exists but cannot be read, a directory that cannot
/// be listed and searched.
fn unreadable(path: &str) -> bool {
    match std::fs::metadata(path) {
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => true,
        Err(_) => false,
        Ok(m) => {
            let mode = if m.is_dir() { 4 | 1 } else { 4 };
            let Ok(c) = std::ffi::CString::new(path) else { return false };
            unsafe { access(c.as_ptr(), mode) != 0 }
        }
    }
}

/// `stamps.matches(path, recorded, lambda _: current)`.
fn matches(path: &str, recorded: Stamp, current: Stamp) -> bool {
    if recorded == Some(UNREADABLE) {
        return unreadable(path);
    }
    current == recorded
}

fn join(directory: &str, name: &str) -> String {
    if directory.is_empty() || directory.ends_with('/') {
        format!("{directory}{name}")
    } else {
        format!("{directory}/{name}")
    }
}

/// `_entries`: the `.py` files and the subdirectories; `None`: cannot be listed.
fn entries(directory: &str) -> Result<Option<(Vec<String>, Vec<String>)>, Fallback> {
    let iterator = match std::fs::read_dir(directory) {
        Ok(it) => it,
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => return Ok(None),
        Err(_) => return Ok(Some((vec![], vec![]))),
    };
    let mut listed = Vec::new();
    for entry in iterator {
        match entry {
            Ok(entry) => listed.push(entry),
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => return Ok(None),
            Err(_) => return Ok(Some((vec![], vec![]))),
        }
    }
    let mut names = Vec::new();
    let mut subdirs = Vec::new();
    for entry in listed {
        let Ok(name) = entry.file_name().into_string() else { return Err(Fallback) };
        let kind = |want_dir: bool| -> bool {
            match entry.file_type() {
                Ok(t) if t.is_symlink() => {
                    std::fs::metadata(entry.path()).map(|m| if want_dir { m.is_dir() } else { m.is_file() }).unwrap_or(false)
                }
                Ok(t) => {
                    if want_dir {
                        t.is_dir()
                    } else {
                        t.is_file()
                    }
                }
                Err(_) => false,
            }
        };
        if !NO_MODEL_DIRS.contains(&name.as_str()) && kind(true) {
            subdirs.push(name.clone());
        }
        if name.ends_with(".py") && kind(false) {
            names.push(name);
        }
    }
    names.sort();
    subdirs.sort();
    Ok(Some((names, subdirs)))
}

/// What the walk of one addon read (merged in load order, as Python walks them).
#[derive(Default)]
struct Part {
    directories: IndexMap<String, Directory>,
    stamps: IndexMap<String, Stamp>,
    order: Vec<(usize, String, String, String)>,
    pending: Vec<(usize, String)>,
}

struct Shared<'k> {
    known: &'k IndexMap<String, Directory>,
    stats: &'k HashMap<String, Status>,
}

impl Shared<'_> {
    fn stat(&self, path: &str) -> Status {
        match self.stats.get(path) {
            Some(found) => *found,
            None => stat(path),
        }
    }

    /// `ClassFileIndex._walk` of a directory of an addon.
    fn walk(&self, part: &mut Part, addon: usize, directory: &str, ancestors: &[(u64, u64)]) -> Result<(), Fallback> {
        let status = self.stat(directory);
        let current = status.map(|s| s.0);
        let identity = status.map(|s| s.1);
        if identity.is_some_and(|i| ancestors.contains(&i)) {
            return Ok(());
        }
        let previous = self.known.get(directory);
        let (recorded, names, subdirs) = match previous {
            Some(previous) if matches(directory, previous.stamp, current) => {
                let mut names: Vec<String> = previous.files.keys().cloned().collect();
                names.sort();
                (previous.stamp, names, previous.subdirs.clone())
            }
            _ => match entries(directory)? {
                None => (Some(UNREADABLE), vec![], vec![]),
                Some((names, subdirs)) => (current, names, subdirs),
            },
        };
        let mut files = IndexMap::new();
        for name in names {
            let path = join(directory, &name);
            let before = previous.and_then(|p| p.files.get(&name)).cloned();
            let found = self.file(part, addon, &path, before);
            part.order.push((addon, directory.to_string(), name.clone(), path));
            files.insert(name, found);
        }
        part.directories.insert(directory.to_string(), Directory { stamp: recorded, files, subdirs: subdirs.clone() });
        let mut inner = ancestors.to_vec();
        if let Some(identity) = identity {
            inner.push(identity);
        }
        for name in subdirs {
            self.walk(part, addon, &join(directory, &name), &inner)?;
        }
        Ok(())
    }

    /// A file as recorded, or one to read (`read_pending`) when it changed.
    fn file(&self, part: &mut Part, addon: usize, path: &str, before: Option<File>) -> File {
        let now = self.stat(path).map(|s| s.0);
        let found = match before {
            Some(before) if matches(path, before.stamp, now) => before,
            _ => {
                part.pending.push((addon, path.to_string()));
                File { stamp: now, models: BTreeSet::new() }
            }
        };
        part.stamps.insert(path.to_string(), found.stamp);
        found
    }
}

pub struct Addon {
    pub name: String,
    pub path: String,
    pub prefix: String,
}

pub struct Walk<'py> {
    addons: Vec<Addon>,
    known: IndexMap<String, Directory>,
    named: Bound<'py, PyAny>, // Python's `_named(addon_index, path)` for what Rust cannot read
    directories: IndexMap<String, Directory>,
    stamps: IndexMap<String, Stamp>,
    order: Vec<(usize, String, String, String)>, // every file in walk order: addon, directory, name, path
    pending: Vec<(usize, String)>,                // the files to read: addon, path
    stats: HashMap<String, Status>,               // taken before the walk, in parallel
}

fn parse_stamp(value: &Bound<'_, PyAny>) -> Stamp {
    let list = value.downcast::<PyList>().ok()?;
    if list.len() != 2 {
        return None;
    }
    let part = |i: usize| -> Option<i64> {
        let item = list.get_item(i).ok()?;
        if !item.get_type().is(&item.py().get_type::<pyo3::types::PyInt>()) {
            return None;
        }
        item.extract().ok()
    };
    Some((part(0)?, part(1)?))
}

/// `class_index._parse` of a kept record.
fn parse(record: &Bound<'_, PyAny>) -> IndexMap<String, Directory> {
    let mut found = IndexMap::new();
    let Ok(record) = record.downcast::<PyDict>() else { return found };
    let Ok(Some(rows)) = record.get_item("directories") else { return found };
    let Ok(rows) = rows.downcast::<PyDict>() else { return found };
    for (path, row) in rows.iter() {
        let Ok(path) = path.extract::<String>() else { continue };
        let Ok(row) = row.downcast::<PyList>() else { continue };
        if row.len() != 3 {
            continue;
        }
        let (recorded, files, subdirs) = (row.get_item(0).unwrap(), row.get_item(1).unwrap(), row.get_item(2).unwrap());
        let (Ok(files), Ok(subdirs)) = (files.downcast::<PyDict>(), subdirs.downcast::<PyList>()) else { continue };
        let mut entries = IndexMap::new();
        for (name, value) in files.iter() {
            let Ok(name) = name.extract::<String>() else { continue };
            let Ok(value) = value.downcast::<PyList>() else { continue };
            if value.len() != 2 {
                continue;
            }
            let Ok(models) = value.get_item(1).unwrap().downcast_into::<PyList>() else { continue };
            let models = models.iter().filter_map(|m| m.downcast::<PyString>().ok().and_then(|s| s.to_str().ok().map(String::from))).collect();
            entries.insert(name, File { stamp: parse_stamp(&value.get_item(0).unwrap()), models });
        }
        let names = subdirs.iter().filter_map(|n| n.downcast::<PyString>().ok().and_then(|s| s.to_str().ok().map(String::from))).collect();
        found.insert(path, Directory { stamp: parse_stamp(&recorded), files: entries, subdirs: names });
    }
    found
}

impl<'py> Walk<'py> {
    pub fn new(addons: Vec<Addon>, record: Option<&Bound<'py, PyAny>>, named: Bound<'py, PyAny>) -> Self {
        let known = record.map(parse).unwrap_or_default();
        Walk {
            addons,
            known,
            named,
            directories: IndexMap::new(),
            stamps: IndexMap::new(),
            order: Vec::new(),
            pending: Vec::new(),
            stats: HashMap::new(),
        }
    }

    fn stat(&self, path: &str) -> Status {
        match self.stats.get(path) {
            Some(found) => *found,
            None => stat(path),
        }
    }

    pub fn run(&mut self) -> PyResult<Result<(), Fallback>> {
        let py = self.named.py();
        // The stats of what the kept record names, in parallel: a walk of kept
        // directories is mostly waiting on them.
        let mut wanted: Vec<String> = Vec::new();
        for (directory, entry) in &self.known {
            wanted.push(directory.clone());
            wanted.extend(entry.files.keys().map(|name| join(directory, name)));
        }
        self.stats = py.allow_threads(|| wanted.into_par_iter().map(|p| { let s = stat(&p); (p, s) }).collect());
        let shared = Shared { known: &self.known, stats: &self.stats };
        let addons = &self.addons;
        let parts: Vec<Result<Part, Fallback>> = py.allow_threads(|| {
            (0..addons.len())
                .into_par_iter()
                .map(|index| {
                    let mut part = Part::default();
                    shared.walk(&mut part, index, &addons[index].path, &[]).map(|()| part)
                })
                .collect()
        });
        for part in parts {
            let Ok(part) = part else { return Ok(Err(Fallback)) };
            for (path, entry) in part.directories {
                self.directories.insert(path, entry); // a directory walked again: the last walk
            }
            for (path, stamp) in part.stamps {
                self.stamps.insert(path, stamp);
            }
            self.order.extend(part.order);
            self.pending.extend(part.pending);
        }
        self.read_pending()?;
        Ok(Ok(()))
    }

    /// Read the files the walk found new or changed: in Rust in parallel, then in Python
    /// those Rust cannot read; their models and stamps go where the walk left them.
    fn read_pending(&mut self) -> PyResult<()> {
        let mut wanted: Vec<(usize, String)> = std::mem::take(&mut self.pending);
        wanted.sort();
        wanted.dedup();
        let addons = &self.addons;
        let py = self.named.py();
        let read: Vec<Option<BTreeSet<String>>> = py.allow_threads(|| {
            wanted
                .par_iter()
                .map(|(addon, path)| {
                    let a = &addons[*addon];
                    crate::named_of(path, &a.path, &a.prefix, &a.name)
                })
                .collect()
        });
        let mut results: HashMap<String, File> = HashMap::new();
        for ((addon, path), found) in wanted.iter().zip(read) {
            let found = match found {
                Some(models) => Some(models),
                None => self.python_named(*addon, path)?,
            };
            let now = self.stat(path).map(|s| s.0);
            let file = match found {
                Some(models) => File { stamp: now, models },
                None => File { stamp: Some(UNREADABLE), models: BTreeSet::new() },
            };
            results.insert(path.clone(), file);
        }
        if results.is_empty() {
            return Ok(());
        }
        for (directory, entry) in self.directories.iter_mut() {
            for (name, file) in entry.files.iter_mut() {
                if let Some(found) = results.get(&join(directory, name)) {
                    *file = found.clone();
                }
            }
        }
        for (path, found) in &results {
            if let Some(stamp) = self.stamps.get_mut(path) {
                *stamp = found.stamp;
            }
        }
        Ok(())
    }

    /// Python's `class_index._named` of a file Rust cannot read.
    fn python_named(&self, addon: usize, path: &str) -> PyResult<Option<BTreeSet<String>>> {
        let found = self.named.call1((addon, path))?;
        if found.is_none() {
            return Ok(None);
        }
        Ok(Some(found.try_iter()?.map(|m| m.and_then(|m| m.extract::<String>())).collect::<PyResult<_>>()?))
    }

    fn stamp_object(py: Python<'py>, stamp: Stamp) -> PyResult<Bound<'py, PyAny>> {
        Ok(match stamp {
            None => py.None().into_bound(py),
            Some((a, b)) => PyTuple::new(py, [a, b])?.into_any(),
        })
    }

    fn stamp_list(py: Python<'py>, stamp: Stamp) -> PyResult<Bound<'py, PyAny>> {
        Ok(match stamp {
            None => py.None().into_bound(py),
            Some((a, b)) => PyList::new(py, [a, b])?.into_any(),
        })
    }

    /// `(found, stamps, directory stamps, record or None)`: the (addon index, path,
    /// models) of the files that name models in walk order, the stamps of the files and
    /// directories as recorded, and the record to keep when it differs from the kept one.
    pub fn result(&self, py: Python<'py>) -> PyResult<Bound<'py, PyTuple>> {
        let found = PyList::empty(py);
        for (addon, directory, name, path) in &self.order {
            let models = &self.directories[directory.as_str()].files[name.as_str()].models;
            if !models.is_empty() {
                found.append((addon, path, models.iter().collect::<Vec<_>>()))?;
            }
        }
        let stamps = PyDict::new(py);
        for (path, stamp) in &self.stamps {
            stamps.set_item(path, Self::stamp_object(py, *stamp)?)?;
        }
        let directory_stamps = PyDict::new(py);
        for (path, entry) in &self.directories {
            directory_stamps.set_item(path, Self::stamp_object(py, entry.stamp)?)?;
        }
        let same = self.directories.len() == self.known.len()
            && self.directories.iter().all(|(path, entry)| {
                self.known.get(path).is_some_and(|k| {
                    k.stamp == entry.stamp
                        && k.subdirs == entry.subdirs
                        && k.files.len() == entry.files.len()
                        && entry.files.iter().all(|(n, f)| k.files.get(n) == Some(f))
                })
            });
        let record = if same {
            py.None().into_bound(py)
        } else {
            let rows = PyDict::new(py);
            for (path, entry) in &self.directories {
                let files = PyDict::new(py);
                for (name, file) in &entry.files {
                    files.set_item(name, PyList::new(py, [Self::stamp_list(py, file.stamp)?, PyList::new(py, &file.models)?.into_any()])?)?;
                }
                let row = PyList::new(py, [Self::stamp_list(py, entry.stamp)?, files.into_any(), PyList::new(py, &entry.subdirs)?.into_any()])?;
                rows.set_item(path, row)?;
            }
            let record = PyDict::new(py);
            record.set_item("directories", rows)?;
            record.into_any()
        };
        PyTuple::new(py, [found.into_any(), stamps.into_any(), directory_stamps.into_any(), record])
    }
}
