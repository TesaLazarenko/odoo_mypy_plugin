//! Rust port of parts of odoo_typing's scanner (pilot): `referenced_models`.
//!
//! Each function takes a file's bytes and gives what the Python scanner gives for its
//! tree, or `None` where the Python side must decide (bytes that are not UTF-8, a
//! source the parser rejects): the caller then falls back to the Python scanner.
use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use rayon::prelude::*;
use ruff_python_ast::visitor::{walk_expr, Visitor};
use ruff_python_ast::{Expr, Stmt};
use std::collections::{HashMap, HashSet};
use std::sync::OnceLock;

use ruff_python_ast::{ModModule, PythonVersion};
use ruff_python_parser::{Mode, ParseOptions, Parsed};

/// The minor version of the Python this module runs in: syntax newer than it is rejected,
/// as `ast.parse` rejects it (the file then goes to the Python scanner).
static PYTHON_MINOR: OnceLock<u8> = OnceLock::new();

pub fn parse(text: &str) -> Option<Parsed<ModModule>> {
    let minor = *PYTHON_MINOR.get().unwrap_or(&11);
    let options = ParseOptions::from(Mode::Module)
        .with_target_version(PythonVersion { major: 3, minor });
    let parsed = ruff_python_parser::parse_unchecked(text, options);
    if !parsed.has_no_syntax_errors() {
        return None;
    }
    let module = parsed.try_into_module()?;
    if minor < 12 {
        // Before 3.12 a quoted (not triple-quoted) f-string is one line: a line break in
        // it (in an expression, PEP 701) is a syntax error that ruff's version check
        // does not report.
        let mut check = OneLineFStrings { text, ok: true };
        check.visit_body(module.suite());
        if !check.ok {
            return None;
        }
    }
    Some(module)
}

struct OneLineFStrings<'t> {
    text: &'t str,
    ok: bool,
}

impl<'a> Visitor<'a> for OneLineFStrings<'_> {
    fn visit_f_string(&mut self, f_string: &'a ruff_python_ast::FString) {
        use ruff_python_ast::str::TripleQuotes;
        use ruff_python_ast::StringFlags;
        use ruff_text_size::Ranged;
        if f_string.flags.triple_quotes() == TripleQuotes::No {
            let source = &self.text[f_string.range().start().to_usize()..f_string.range().end().to_usize()];
            let bytes = source.as_bytes();
            for (i, &c) in bytes.iter().enumerate() {
                if (c == b'\n' || c == b'\r') && (i == 0 || bytes[i - 1] != b'\\') {
                    self.ok = false;
                }
            }
        }
        ruff_python_ast::visitor::walk_f_string(self, f_string);
    }
}

mod guards;
mod super_returns;
mod class_bodies;
mod assigned;
mod value;
mod walker;
mod text;
mod xml;
mod decode;
mod falsy;
mod classwalk;
mod registry;
mod fieldmerge;
mod encode;
mod canonical;

const RELATIONAL_FIELDS: [&str; 3] = ["Many2one", "One2many", "Many2many"];
const LOG_ACCESS_USERS: [&str; 2] = ["create_uid", "write_uid"];

fn env_record(attr: &str) -> Option<&'static str> {
    match attr {
        "user" => Some("res.users"),
        "company" | "companies" => Some("res.company"),
        _ => None,
    }
}

/// `[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+` as a full match.
fn is_model_name(name: &str) -> bool {
    let bytes = name.as_bytes();
    if bytes.is_empty() || !bytes[0].is_ascii_lowercase() {
        return false;
    }
    let word = |c: u8| c.is_ascii_lowercase() || c.is_ascii_digit() || c == b'_';
    let mut parts = name.split('.');
    let first = parts.next().unwrap();
    if !first.bytes().all(word) {
        return false;
    }
    let mut dotted = 0;
    for part in parts {
        if part.is_empty() || !part.bytes().all(word) {
            return false;
        }
        dotted += 1;
    }
    dotted > 0
}

fn string_literal(expr: &Expr) -> Option<String> {
    match expr {
        Expr::StringLiteral(literal) => Some(literal.value.to_str().to_string()),
        _ => None,
    }
}

fn callee_name(expr: &Expr) -> Option<&str> {
    match expr {
        Expr::Attribute(attribute) => Some(attribute.attr.as_str()),
        Expr::Name(name) => Some(name.id.as_str()),
        _ => None,
    }
}

fn is_env(expr: &Expr) -> bool {
    match expr {
        Expr::Attribute(attribute) => attribute.attr.as_str() == "env",
        Expr::Name(name) => name.id.as_str() == "env",
        _ => false,
    }
}

fn is_final(annotation: &Expr) -> bool {
    let annotation = match annotation {
        Expr::Subscript(subscript) => &*subscript.value,
        other => other,
    };
    callee_name(annotation) == Some("Final")
}

fn string_constants(body: &[Stmt]) -> HashMap<String, String> {
    let mut constants = HashMap::new();
    for statement in body {
        if let Stmt::AnnAssign(assign) = statement {
            if !is_final(&assign.annotation) {
                continue;
            }
            if let (Expr::Name(name), Some(value)) = (&*assign.target, &assign.value) {
                if let Some(text) = string_literal(value) {
                    constants.insert(name.id.as_str().to_string(), text);
                }
            }
        }
    }
    constants
}

struct Referenced<'c> {
    constants: &'c HashMap<String, String>,
    models: HashSet<String>,
}

impl<'a> Visitor<'a> for Referenced<'_> {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        guards::walk_stmt_once(self, stmt);
    }
    fn visit_expr(&mut self, expr: &'a Expr) {
        match expr {
            Expr::Subscript(subscript) => {
                let name = match &*subscript.slice {
                    Expr::Name(key) => self.constants.get(key.id.as_str()).cloned(),
                    key => string_literal(key),
                };
                if let Some(name) = name {
                    if !name.is_empty() && (is_env(&subscript.value) || is_model_name(&name)) {
                        self.models.insert(name);
                    }
                }
            }
            Expr::Call(call) => {
                if callee_name(&call.func).is_some_and(|n| RELATIONAL_FIELDS.contains(&n)) {
                    let comodel = match call.arguments.args.first() {
                        Some(first) => string_literal(first),
                        None => call
                            .arguments
                            .keywords
                            .iter()
                            .find(|k| k.arg.as_ref().is_some_and(|a| a.as_str() == "comodel_name"))
                            .and_then(|k| string_literal(&k.value)),
                    };
                    if let Some(name) = comodel {
                        if !name.is_empty() {
                            self.models.insert(name);
                        }
                    }
                }
            }
            Expr::Attribute(attribute) => {
                let attr = attribute.attr.as_str();
                if let (Some(model), true) = (env_record(attr), is_env(&attribute.value)) {
                    self.models.insert(model.to_string());
                } else if is_env(&attribute.value) {
                    self.models.insert(attr.to_string());
                } else if LOG_ACCESS_USERS.contains(&attr) {
                    self.models.insert("res.users".to_string());
                }
            }
            _ => {}
        }
        walk_expr(self, expr);
    }
}

pub fn source_text(source: &[u8]) -> Option<&str> {
    let source = source.strip_prefix(b"\xef\xbb\xbf").unwrap_or(source);
    std::str::from_utf8(source).ok()
}

fn referenced(source: &[u8]) -> Option<HashSet<String>> {
    let text = source_text(source)?;
    let parsed = parse(text)?;
    Some(referenced_body(parsed.suite()))
}

pub fn referenced_body(body: &[Stmt]) -> HashSet<String> {
    let constants = string_constants(body);
    let mut visitor = Referenced { constants: &constants, models: HashSet::new() };
    visitor.visit_body(body);
    visitor.models
}

static NAME_TEST: OnceLock<regex::bytes::Regex> = OnceLock::new();
static ANNOTATION: OnceLock<regex::bytes::Regex> = OnceLock::new();

/// `NAME_TEST.search(source)` of the walker: the source compares a `_name`.
pub fn name_test(source: &[u8]) -> bool {
    NAME_TEST
        .get_or_init(|| regex::bytes::Regex::new(r"(?-u)\w\._name\s*(?:[=!]=|(?:not\s+)?in\b)").unwrap())
        .is_match(source)
}

/// `annotation_aliases(source)`: the `(kind, alias)` of the `odoo.<kind>.*` names.
pub fn annotation_aliases(source: &[u8]) -> Vec<(String, String)> {
    let needles: [&[u8]; 3] = [b"odoo.values.", b"odoo.model.", b"odoo.domain."];
    if !needles.iter().any(|n| source.windows(n.len()).any(|w| w == *n)) {
        return vec![];
    }
    let pattern = ANNOTATION.get_or_init(|| {
        regex::bytes::Regex::new(r"(?-u)odoo\.(values|model|domain)\.([A-Za-z0-9_.]*[A-Za-z0-9_])").unwrap()
    });
    let mut found: Vec<(String, String)> = pattern
        .captures_iter(source)
        .map(|c| (String::from_utf8_lossy(&c[1]).into_owned(), String::from_utf8_lossy(&c[2]).into_owned()))
        .collect();
    found.sort();
    found.dedup();
    found
}

/// The models a file refers to (`odoo_typing.scanner.referenced.referenced_models`).
#[pyfunction]
fn referenced_models(source: &[u8]) -> Option<HashSet<String>> {
    referenced(source)
}

/// `referenced_models` of many files, in `threads` threads without the GIL.
#[pyfunction]
fn referenced_models_many(
    py: Python<'_>,
    sources: Vec<PyBackedBytes>,
    threads: usize,
) -> PyResult<Vec<Option<HashSet<String>>>> {
    py.allow_threads(|| {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .build()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
        Ok(pool.install(|| sources.par_iter().map(|s| referenced(s.as_ref())).collect()))
    })
}

fn guards_of(source: &[u8]) -> Option<Vec<guards::Guard>> {
    let text = source_text(source)?;
    let parsed = parse(text)?;
    let lines = guards::Lines::new(text);
    Some(guards::model_guards(parsed.suite(), &lines))
}

/// The guards of a file (`odoo_typing.guards.model_guards`): (model, first, last).
#[pyfunction]
fn model_guards(source: &[u8]) -> Option<Vec<guards::Guard>> {
    guards_of(source)
}

/// `model_guards` of many files, in `threads` threads without the GIL.
#[pyfunction]
fn model_guards_many(
    py: Python<'_>,
    sources: Vec<PyBackedBytes>,
    threads: usize,
) -> PyResult<Vec<Option<Vec<guards::Guard>>>> {
    py.allow_threads(|| {
        let pool = rayon::ThreadPoolBuilder::new()
            .num_threads(threads)
            .build()
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e.to_string()))?;
        Ok(pool.install(|| sources.par_iter().map(|s| guards_of(s.as_ref())).collect()))
    })
}

/// Every class of a file (any depth), with its name line and name.
fn all_classes<'a>(body: &'a [Stmt]) -> Vec<&'a ruff_python_ast::StmtClassDef> {
    struct Classes<'a>(Vec<&'a ruff_python_ast::StmtClassDef>);
    impl<'a> Visitor<'a> for Classes<'a> {
        fn visit_stmt(&mut self, stmt: &'a Stmt) {
            if let Stmt::ClassDef(c) = stmt {
                self.0.push(c);
            }
            guards::walk_stmt_once(self, stmt);
        }
    }
    let mut found = Classes(vec![]);
    found.visit_body(body);
    found.0
}

/// `super_returns` of every class of a file: (class line, class name, methods).
#[pyfunction]
fn super_returns_by_class(source: &[u8]) -> Option<Vec<(u32, String, Vec<String>)>> {
    let text = source_text(source)?;
    let parsed = parse(text)?;
    let lines = guards::Lines::new(text);
    Some(
        all_classes(parsed.suite())
            .into_iter()
            .map(|c| (guards::name_line(&lines, &c.name), c.name.as_str().to_string(), super_returns::super_returns(c)))
            .collect(),
    )
}

type Dispatch = (String, String, Vec<String>);
type PassedEntry = (String, String, Vec<String>, String);
type ClassBody = (u32, String, Vec<String>, Vec<String>, Vec<String>, Vec<Dispatch>, Vec<PassedEntry>);

/// The class-body parts of every class of a file: (line, name, methods, comodels,
/// value_keys, dispatches, passed).
#[pyfunction]
fn class_bodies_by_class(source: &[u8]) -> Option<Vec<ClassBody>> {
    let text = source_text(source)?;
    let parsed = parse(text)?;
    let lines = guards::Lines::new(text);
    Some(
        all_classes(parsed.suite())
            .into_iter()
            .map(|c| {
                (
                    guards::name_line(&lines, &c.name),
                    c.name.as_str().to_string(),
                    class_bodies::defined_methods(c),
                    class_bodies::declared_comodels(c),
                    class_bodies::read_value_keys(c),
                    class_bodies::record_dispatches(c),
                    class_bodies::passed_containers(c),
                )
            })
            .collect(),
    )
}

type AssignedOut = (u32, String, Vec<(String, String, Vec<String>)>, Vec<String>, Vec<(String, Vec<String>)>, Vec<(String, Vec<String>)>);

/// `assigned_values` of every class of a file: (line, name, known, unknown, through, filled).
#[pyfunction]
fn assigned_by_class(source: &[u8]) -> Option<Vec<AssignedOut>> {
    let text = source_text(source)?;
    let parsed = parse(text)?;
    let lines = guards::Lines::new(text);
    Some(
        all_classes(parsed.suite())
            .into_iter()
            .map(|c| {
                let a = assigned::assigned_values(c);
                (guards::name_line(&lines, &c.name), c.name.as_str().to_string(), a.known, a.unknown, a.through, a.filled)
            })
            .collect(),
    )
}

type FieldOut = (
    (bool, String, String),
    (Option<String>, Option<String>, Option<Vec<String>>, Option<String>, Option<Vec<String>>),
    (Option<String>, Option<String>, Option<String>, Option<String>),
    (Option<bool>, Option<bool>, Option<bool>, Option<bool>, Option<bool>, Option<bool>),
    (Vec<String>, Vec<String>, Vec<walker::Positional>),
);

fn field_out(f: walker::FieldInfo) -> FieldOut {
    (
        (f.unlinked, f.name, f.field_type),
        (f.comodel, f.inverse_name, f.selection, f.selection_method, f.selection_add),
        (f.related, f.compute, f.inverse, f.search),
        (f.readonly, f.required, f.store, f.delegate, f.compute_sql, f.attachment),
        (f.dynamic, f.given, f.positional),
    )
}

fn contributions_out<'py>(py: Python<'py>, contributions: Vec<walker::Contribution>) -> PyResult<Bound<'py, pyo3::types::PyList>> {
    let out = pyo3::types::PyList::empty(py);
    for c in contributions {
        let fields: Vec<FieldOut> = c.fields.into_iter().map(field_out).collect();
        let item = (
            (c.addon, c.module, c.class_name, c.model, c.parents, c.position),
            (c.comodels, fields, c.methods, c.delegates, c.inherits),
            (c.log_access, c.parent_store, c.abstract_),
            (c.value_keys, c.dispatches, c.passed),
            (c.assigned, c.assigned_any, c.assigned_through, c.filled, c.super_returns),
        );
        out.append(item.into_pyobject(py)?)?;
    }
    Ok(out)
}

/// `walker.scan_file_classes`: the model classes of one file of an addon read on its
/// own; `None` where Python must read it.
#[pyfunction]
fn file_classes(py: Python<'_>, path: &str, addon_path: &str, prefix: &str, name: &str) -> PyResult<Option<PyObject>> {
    let mut walker = walker::Walker::new(addon_path, prefix, name, (0, name.to_string()));
    walker.one_file = true;
    match walker.file_classes(std::path::Path::new(path)) {
        Some(found) => Ok(Some(contributions_out(py, found)?.into_any().unbind())),
        None => Ok(None),
    }
}

/// The Python files of an addon, scanned in import order (`walker.scan_addon` without
/// the data files): (modules, stamps), or `None` where Python must scan the addon.
#[pyfunction]
fn scan_addon_modules(
    py: Python<'_>,
    path: &str,
    prefix: &str,
    name: &str,
    load_key: (i64, String),
) -> PyResult<Option<(PyObject, PyObject)>> {
    let mut walker = walker::Walker::new(path, prefix, name, load_key);
    walker.visit_module(prefix, std::path::Path::new(path));
    if walker.failed {
        return Ok(None);
    }
    let stamps = pyo3::types::PyDict::new(py);
    for (k, v) in &walker.stamps {
        stamps.set_item(k, v)?;
    }
    let modules = pyo3::types::PyList::empty(py);
    for m in std::mem::take(&mut walker.scans) {
        let contributions = contributions_out(py, m.contributions)?;
        let classes: Vec<(String, Vec<String>, bool, bool, Vec<String>)> =
            m.classes.into_iter().map(|k| (k.name, k.bases, k.converts, k.arguments, k.passes)).collect();
        let item = (m.module, m.path, contributions, m.referenced, m.guards, m.annotations, classes);
        modules.append(item.into_pyobject(py)?)?;
    }
    Ok(Some((modules.into_any().unbind(), stamps.into_any().unbind())))
}

/// What a file refers to (`FileReferences`): its models, guards and annotation aliases,
/// from one parse; `None` where Python must read it.
#[pyfunction]
fn file_references(source: &[u8]) -> Option<(Vec<String>, Vec<guards::Guard>, Vec<(String, String)>)> {
    let text = source_text(source)?;
    let parsed = parse(text)?;
    let lines = guards::Lines::new(text);
    Some((
        referenced_body(parsed.suite()).into_iter().collect(),
        guards::model_guards(parsed.suite(), &lines),
        annotation_aliases(source),
    ))
}

/// `visible._defined_names`: the models of the `_name = "..."` of an addon's files.
#[pyfunction]
fn defined_names(py: Python<'_>, directory: &str) -> Vec<String> {
    py.allow_threads(|| text::defined_names(std::path::Path::new(directory)).into_iter().collect())
}

/// `defined_names` of many addon directories at once, in parallel, in their order.
#[pyfunction]
fn defined_names_many(py: Python<'_>, directories: Vec<String>) -> Vec<Vec<String>> {
    py.allow_threads(|| {
        directories
            .par_iter()
            .map(|directory| text::defined_names(std::path::Path::new(directory)).into_iter().collect())
            .collect()
    })
}

/// `class_index._named`: the models of a file's `_name` / `_inherit`; `None` where
/// Python must read it (it cannot be read, or the scan of its classes is needed and
/// cannot be done here).
#[pyfunction]
fn named_models(path: &str, addon_path: &str, prefix: &str, name: &str) -> Option<Vec<String>> {
    Some(named_of(path, addon_path, prefix, name)?.into_iter().collect())
}

pub fn named_of(path: &str, addon_path: &str, prefix: &str, name: &str) -> Option<std::collections::BTreeSet<String>> {
    let data = std::fs::read(path).ok()?;
    let (mut names, evaluate) = text::named_literals(&data);
    if evaluate {
        let mut walker = walker::Walker::new(addon_path, prefix, name, (0, name.to_string()));
        walker.one_file = true;
        let classes = walker.file_classes(std::path::Path::new(path))?;
        for c in classes {
            names.insert(c.model);
            names.extend(c.parents);
        }
    }
    Some(names)
}

/// `edges.imported_names` of a file; `None` where Python must parse it.
#[pyfunction]
fn imported_names(path: &str, package: &str) -> Option<Vec<String>> {
    let data = std::fs::read(path).ok()?;
    let text_ = source_text(&data)?;
    let parsed = parse(text_)?;
    Some(text::imported_names(parsed.suite(), package).into_iter().collect())
}

/// `xmlids._file_ids`: the (id, model) of a data file; `None` where Python must read
/// it; `False` when it cannot be read.
#[pyfunction]
fn data_file_ids(py: Python<'_>, path: &str, addon: &str) -> PyResult<PyObject> {
    Ok(match xml::file_ids(path, addon) {
        xml::Ids::Fallback => py.None(),
        xml::Ids::Unreadable => false.into_pyobject(py)?.to_owned().into_any().unbind(),
        xml::Ids::Found(ids) => ids.into_pyobject(py)?.into_any().unbind(),
    })
}

/// `scan_store._decode` of a stored value; `ValueError` for a value `encode` did not make.
#[pyfunction]
fn decode_value<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    classes: Bound<'py, pyo3::types::PyDict>,
    path: Bound<'py, PyAny>,
    error: Bound<'py, PyAny>,
    frozen: Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let builders = decode::Builders { classes, path, error, frozen };
    decode::decode(py, value, &builders, 0)
}

/// `ClassFileIndex._index` of one record: `addons` are (name, path, prefix) in load
/// order, `record` the kept one (or `None`), `named(addon index, path)` Python's reading
/// of a file Rust cannot read. `None` where Python must walk (a name not in UTF-8).
#[pyfunction]
#[pyo3(signature = (addons, record, named))]
fn class_walk<'py>(
    py: Python<'py>,
    addons: Vec<(String, String, String)>,
    record: Option<Bound<'py, PyAny>>,
    named: Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, pyo3::types::PyTuple>>> {
    let addons = addons.into_iter().map(|(name, path, prefix)| classwalk::Addon { name, path, prefix }).collect();
    let mut walk = classwalk::Walk::new(addons, record.as_ref(), named);
    match walk.run()? {
        Err(classwalk::Fallback) => Ok(None),
        Ok(()) => Ok(Some(walk.result(py)?)),
    }
}

/// `Linearization._registry_bases` over the parents of the classes: the class indexes
/// (ints) and the parent models (strs) of the registry class's bases, in order.
#[pyfunction]
fn registry_bases<'py>(
    py: Python<'py>,
    model: &str,
    parents_of: Vec<Vec<String>>,
    dropped: HashSet<String>,
) -> PyResult<Bound<'py, pyo3::types::PyList>> {
    let nodes = registry::registry_bases(model, &parents_of, &dropped);
    let out = pyo3::types::PyList::empty(py);
    for node in nodes {
        match node {
            registry::Node::Class(index) => out.append(index)?,
            registry::Node::Model(name) => out.append(name)?,
        }
    }
    Ok(out)
}

/// `graphs.c3_merge` over node numbers: (merged, conflict).
#[pyfunction]
fn c3_merge(sequences: Vec<Vec<u32>>) -> (Vec<u32>, bool) {
    registry::c3_merge(&sequences)
}

/// `model_fields.merge_definitions` of each group of definitions (base first): the
/// merged `FieldInfo` (the tip itself where it is the same), `None` where Python merges.
#[pyfunction]
#[allow(clippy::too_many_arguments)]
fn merge_fields<'py>(
    groups: Vec<Vec<Bound<'py, PyAny>>>,
    field_info: Bound<'py, PyAny>,
    frozen: Bound<'py, PyAny>,
    parents: HashMap<String, String>,
    overridable: Vec<String>,
    any_argument: String,
) -> PyResult<Vec<Option<Bound<'py, PyAny>>>> {
    let rules = fieldmerge::Rules { parents, overridable, any_argument };
    groups.iter().map(|group| fieldmerge::merge(&rules, group, &field_info, &frozen)).collect()
}

/// `json.dumps(scan_store.encode(value), separators=(",", ":"))`: `fields_of` gives a
/// dataclass's fields, `json_dumps` writes floats and what is rare, `encode` is Python's
/// for a set of other values than plain strings.
#[pyfunction]
fn encode_json<'py>(
    value: &Bound<'py, PyAny>,
    fields_of: Bound<'py, PyAny>,
    source_error: Bound<'py, pyo3::types::PyType>,
    path: Bound<'py, pyo3::types::PyType>,
    json_dumps: Bound<'py, PyAny>,
    encode: Bound<'py, PyAny>,
) -> PyResult<String> {
    let mut out = String::new();
    encode::Encoder::new(fields_of, source_error, path, json_dumps, encode).encode(value, &mut out)?;
    Ok(out)
}

/// `json.dumps(fingerprint._canonical(value), separators=(",", ":"))`: `fields_of` gives
/// the fingerprinted fields of a dataclass (`None` for another class), `json_dumps` writes
/// what is rare here (a big int, a float, a string with a lone surrogate).
#[pyfunction]
fn canonical_json<'py>(
    value: &Bound<'py, PyAny>,
    fields_of: Bound<'py, PyAny>,
    path: Bound<'py, pyo3::types::PyType>,
    json_dumps: Bound<'py, PyAny>,
) -> PyResult<String> {
    canonical::Canonical::new(fields_of, path, json_dumps).text(value)
}

/// `falsy_guards.FalsyGuards` (read once per file and function, as Python keeps them).
#[pyclass]
struct FalsyGuards {
    state: falsy::State,
}

#[pymethods]
impl FalsyGuards {
    #[new]
    fn new() -> Self {
        FalsyGuards { state: falsy::State::default() }
    }

    /// `(falsy, booleans, none_only_for, callable_first)` of function `name` of a file, a
    /// method of top-level class `owner` ("" for a module function).
    fn of(&mut self, path: &str, owner: &str, name: &str) -> (Vec<String>, Vec<String>, Vec<String>, bool) {
        let found = self.state.of(path, owner, name);
        (
            found.falsy.into_iter().collect(),
            found.booleans.into_iter().collect(),
            found.none_only_for.into_iter().collect(),
            found.callable_first,
        )
    }
}

#[pymodule]
fn odoo_scan_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let minor: u8 = m.py().import("sys")?.getattr("version_info")?.getattr("minor")?.extract()?;
    let _ = PYTHON_MINOR.set(minor);
    m.add_function(wrap_pyfunction!(referenced_models, m)?)?;
    m.add_function(wrap_pyfunction!(referenced_models_many, m)?)?;
    m.add_function(wrap_pyfunction!(model_guards, m)?)?;
    m.add_function(wrap_pyfunction!(model_guards_many, m)?)?;
    m.add_function(wrap_pyfunction!(super_returns_by_class, m)?)?;
    m.add_function(wrap_pyfunction!(class_bodies_by_class, m)?)?;
    m.add_function(wrap_pyfunction!(assigned_by_class, m)?)?;
    m.add_function(wrap_pyfunction!(scan_addon_modules, m)?)?;
    m.add_function(wrap_pyfunction!(file_references, m)?)?;
    m.add_function(wrap_pyfunction!(file_classes, m)?)?;
    m.add_function(wrap_pyfunction!(defined_names, m)?)?;
    m.add_function(wrap_pyfunction!(named_models, m)?)?;
    m.add_function(wrap_pyfunction!(imported_names, m)?)?;
    m.add_function(wrap_pyfunction!(data_file_ids, m)?)?;
    m.add_function(wrap_pyfunction!(decode_value, m)?)?;
    m.add_class::<FalsyGuards>()?;
    m.add_function(wrap_pyfunction!(registry_bases, m)?)?;
    m.add_function(wrap_pyfunction!(c3_merge, m)?)?;
    m.add_function(wrap_pyfunction!(merge_fields, m)?)?;
    m.add_function(wrap_pyfunction!(encode_json, m)?)?;
    m.add_function(wrap_pyfunction!(canonical_json, m)?)?;
    m.add_function(wrap_pyfunction!(defined_names_many, m)?)?;
    m.add_function(wrap_pyfunction!(class_walk, m)?)?;
    Ok(())
}
