//! Port of `odoo_typing.scanner.walker` (the Python files of an addon, in import
//! order), with `fields`, `field_classes` and the attribute parts of `class_bodies`.
//! Any file the port cannot handle makes the whole addon fall back to Python (`None`).
use crate::value::{evaluate, Chain, Scope, V};
use crate::{assigned, class_bodies, guards, super_returns};
use indexmap::IndexMap;
use ruff_python_ast::{ExceptHandler, Expr, ExprCall, Stmt, StmtClassDef, StmtFunctionDef};
use std::cell::RefCell;
use std::collections::{BTreeSet, HashMap, HashSet};
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::rc::Rc;

const RELATIONAL: [&str; 3] = ["Many2one", "One2many", "Many2many"];
const ODOO_FIELDS: &str = "odoo.fields.";
const PARTIAL: &str = "functools.partial";
const ANY_ARGUMENT: &str = "*";

fn positional_parameters(field_type: &str) -> Option<&'static [&'static str]> {
    Some(match field_type {
        "Many2one" => &["comodel_name", "string"],
        "One2many" => &["comodel_name", "inverse_name", "string"],
        "Many2many" => &["comodel_name", "relation", "column1", "column2", "string"],
        "Selection" | "Reference" => &["selection", "string"],
        "Float" => &["string", "digits"],
        "Monetary" => &["string", "currency_field"],
        _ => return None,
    })
}
fn positional_attribute(parameter: &str) -> Option<&'static str> {
    match parameter {
        "comodel_name" => Some("comodel"),
        "inverse_name" => Some("inverse_name"),
        "selection" => Some("selection"),
        _ => None,
    }
}
const FIELD_ATTRS: [&str; 15] = [
    "comodel", "inverse_name", "selection", "selection_method", "selection_add", "related",
    "compute", "inverse", "search", "readonly", "required", "store", "delegate",
    "compute_sql", "attachment",
];
fn argument_attribute(parameter: &str) -> Option<&str> {
    if parameter == "comodel_name" {
        return Some("comodel");
    }
    FIELD_ATTRS.iter().find(|a| **a == parameter).copied()
}

#[derive(Clone, Debug)]
pub struct FieldClass {
    pub name: String,
    pub bases: Vec<String>,
    pub converts: bool,
    pub arguments: bool,
    pub passes: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct FieldKind {
    name: String,
    base: String,
    converts: bool,
    arguments: bool,
    passes: BTreeSet<String>,
}

impl FieldKind {
    fn field_type(&self) -> String {
        if self.converts && !RELATIONAL.contains(&self.base.as_str()) { self.name.clone() } else { self.base.clone() }
    }
}

pub type Positional = (Option<String>, Option<Vec<String>>, Option<String>);

#[derive(Clone, Debug, Default)]
pub struct FieldInfo {
    pub unlinked: bool,
    pub name: String,
    pub field_type: String,
    pub comodel: Option<String>,
    pub inverse_name: Option<String>,
    pub selection: Option<Vec<String>>,
    pub selection_method: Option<String>,
    pub selection_add: Option<Vec<String>>,
    pub related: Option<String>,
    pub compute: Option<String>,
    pub inverse: Option<String>,
    pub search: Option<String>,
    pub readonly: Option<bool>,
    pub required: Option<bool>,
    pub store: Option<bool>,
    pub delegate: Option<bool>,
    pub compute_sql: Option<bool>,
    pub attachment: Option<bool>,
    pub dynamic: Vec<String>,
    pub given: Vec<String>,
    pub positional: Vec<Positional>,
}

impl FieldInfo {
    fn is_set(&self, attribute: &str) -> bool {
        match attribute {
            "comodel" => self.comodel.is_some(),
            "inverse_name" => self.inverse_name.is_some(),
            "selection" => self.selection.is_some(),
            "selection_method" => self.selection_method.is_some(),
            "selection_add" => self.selection_add.is_some(),
            "related" => self.related.is_some(),
            "compute" => self.compute.is_some(),
            "inverse" => self.inverse.is_some(),
            "search" => self.search.is_some(),
            "readonly" => self.readonly.is_some(),
            "required" => self.required.is_some(),
            "store" => self.store.is_some(),
            "delegate" => self.delegate.is_some(),
            "compute_sql" => self.compute_sql.is_some(),
            "attachment" => self.attachment.is_some(),
            _ => false,
        }
    }
}

pub struct Contribution {
    pub addon: String,
    pub module: String,
    pub class_name: String,
    pub model: String,
    pub parents: Vec<String>,
    pub position: (i64, String, i64),
    pub comodels: Vec<String>,
    pub fields: Vec<FieldInfo>,
    pub methods: Vec<String>,
    pub delegates: Vec<String>,
    pub inherits: Vec<(String, String)>,
    pub log_access: Option<bool>,
    pub parent_store: Option<bool>,
    pub abstract_: Option<bool>,
    pub value_keys: Vec<String>,
    pub dispatches: Vec<(String, String, Vec<String>)>,
    pub passed: Vec<(String, String, Vec<String>, String)>,
    pub assigned: Vec<(String, String, Vec<String>)>,
    pub assigned_any: Vec<String>,
    pub assigned_through: Vec<(String, Vec<String>)>,
    pub filled: Vec<(String, Vec<String>)>,
    pub super_returns: Vec<String>,
}

pub struct ModuleScan {
    pub module: String,
    pub path: String,
    pub contributions: Vec<Contribution>,
    pub referenced: Vec<String>,
    pub guards: Vec<guards::Guard>,
    pub annotations: Vec<(String, String)>,
    pub classes: Vec<FieldClass>,
}

type Names = Rc<RefCell<HashMap<String, V>>>;
type Origins = Rc<RefCell<IndexMap<String, String>>>;

struct File {
    module: String,
    package: String,
    names: Names,
    origins: Origins,
    contributions: Vec<Contribution>,
    classes: IndexMap<String, FieldClass>,
    tests_names: bool,
}

pub struct Walker {
    path: PathBuf,
    prefix: String,
    name: String,
    load_key: (i64, String),
    roots: Vec<String>,
    pub stamps: IndexMap<String, (i64, i64)>,
    seen: HashSet<PathBuf>,
    classes: HashSet<String>,
    names: HashMap<String, Names>,
    origins: HashMap<String, Origins>,
    field_classes: HashMap<String, FieldClass>,
    pub scans: Vec<ModuleScan>,
    pub failed: bool,
    /// `_FileWalker`: one file on its own, no import followed, every origin linkable.
    pub one_file: bool,
}

fn stamp(path: &Path) -> Option<(i64, i64)> {
    let m = std::fs::metadata(path).ok()?;
    Some((m.mtime() * 1_000_000_000 + m.mtime_nsec(), m.size() as i64))
}

fn callee_name(e: &Expr) -> Option<&str> {
    match e {
        Expr::Attribute(a) => Some(a.attr.as_str()),
        Expr::Name(n) => Some(n.id.as_str()),
        _ => None,
    }
}

fn origin_of(node: &Expr, origins: &IndexMap<String, String>) -> Option<String> {
    match node {
        Expr::Name(n) => origins.get(n.id.as_str()).cloned(),
        Expr::Attribute(a) => origin_of(&a.value, origins).map(|o| format!("{o}.{}", a.attr.as_str())),
        Expr::Subscript(s) => origin_of(&s.value, origins),
        _ => None,
    }
}

fn odoo_class(origin: &str) -> Option<String> {
    let name = origin.strip_prefix(ODOO_FIELDS)?;
    (!name.contains('.') && name.chars().next().is_some_and(|c| c.is_uppercase())).then(|| name.to_string())
}

fn field_kind(origin: &str, lookup: &HashMap<String, FieldClass>, seen: &BTreeSet<String>) -> Option<FieldKind> {
    if let Some(name) = odoo_class(origin) {
        return Some(FieldKind { name: name.clone(), base: name, converts: false, arguments: false, passes: BTreeSet::new() });
    }
    if seen.contains(origin) {
        return None;
    }
    let described = lookup.get(origin)?;
    let mut more = seen.clone();
    more.insert(origin.to_string());
    for base in &described.bases {
        if let Some(kind) = field_kind(base, lookup, &more) {
            let mut passes = kind.passes.clone();
            passes.extend(described.passes.iter().cloned());
            return Some(FieldKind {
                name: described.name.clone(),
                base: kind.base,
                converts: described.converts || kind.converts,
                arguments: described.arguments || kind.arguments,
                passes,
            });
        }
    }
    None
}

fn passed_names(call: &ExprCall, has_positional: bool) -> Vec<String> {
    let mut names: Vec<String> = call.arguments.keywords.iter().filter_map(|k| k.arg.as_ref().map(|a| a.as_str().to_string())).collect();
    if has_positional {
        names.push("*".to_string());
    }
    names
}

enum ClassOf {
    Kind(FieldKind),
    Name(String),
}

fn executed_blocks(node: &Stmt) -> Vec<&[Stmt]> {
    match node {
        Stmt::If(i) => {
            let tc = match &*i.test {
                Expr::Name(n) => n.id.as_str() == "TYPE_CHECKING",
                Expr::Attribute(a) => a.attr.as_str() == "TYPE_CHECKING",
                _ => false,
            };
            // Python: [orelse] or [body, orelse], where orelse is the elif chain as a nested
            // If (walked as a statement) or the else body.
            let orelse = orelse_block(i, 0);
            if tc { vec![orelse] } else { vec![&i.body, orelse] }
        }
        Stmt::Try(t) => {
            let mut b: Vec<&[Stmt]> = vec![&t.body];
            for ExceptHandler::ExceptHandler(h) in &t.handlers {
                b.push(&h.body);
            }
            b.push(&t.orelse);
            b.push(&t.finalbody);
            b
        }
        Stmt::With(w) => vec![&w.body],
        Stmt::Match(m) => m.cases.iter().map(|c| &c.body[..]).collect(),
        _ => vec![],
    }
}

/// The else body of an `If` (an `elif` is handled by `walk_elif`).
fn orelse_block(i: &ruff_python_ast::StmtIf, _n: usize) -> &[Stmt] {
    match i.elif_else_clauses.first() {
        Some(c) if c.test.is_none() => &c.body,
        _ => &[],
    }
}

fn keys_of(value: &V) -> Option<Vec<String>> {
    let items = value.items()?;
    let mut keys = vec![];
    for pair in items {
        match pair.items().and_then(|p| p.first()) {
            Some(V::Str(k)) => keys.push(k.clone()),
            _ => return None,
        }
    }
    Some(keys)
}

impl Walker {
    pub fn new(path: &str, prefix: &str, name: &str, load_key: (i64, String)) -> Self {
        Walker {
            path: PathBuf::from(path),
            prefix: prefix.to_string(),
            name: name.to_string(),
            load_key,
            roots: vec![format!("odoo.addons.{name}"), prefix.to_string()],
            stamps: IndexMap::new(),
            seen: HashSet::new(),
            classes: HashSet::new(),
            names: HashMap::new(),
            origins: HashMap::new(),
            field_classes: HashMap::new(),
            scans: vec![],
            failed: false,
            one_file: false,
        }
    }

    fn look(&mut self, path: &Path) {
        let key = path.to_string_lossy().into_owned();
        if !self.stamps.contains_key(&key) {
            if let Some(s) = stamp(path) {
                self.stamps.insert(key, s);
            }
        }
    }

    fn is_file(&mut self, path: &Path) -> bool {
        match std::fs::metadata(path) {
            Ok(m) => m.is_file(),
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                self.failed = true;
                false
            }
            Err(_) => false,
        }
    }

    pub fn visit_module(&mut self, module: &str, base: &Path) {
        if self.failed {
            return;
        }
        if base != self.path {
            if let Some(parent) = base.parent() {
                self.look(parent);
            }
        }
        self.look(base);
        let init = base.join("__init__.py");
        if self.is_file(&init) {
            self.visit(&init, module, module);
        } else {
            let file = base.with_extension("py");
            let package = module.rsplit_once('.').map(|(p, _)| p).unwrap_or("").to_string();
            // `Path.with_suffix` replaces a suffix the name has (`a.b` -> `a.py`)
            self.visit(&file, module, &package);
        }
    }

    fn visit(&mut self, path: &Path, module: &str, package: &str) {
        if self.failed || self.seen.contains(path) {
            return;
        }
        self.look(path);
        if !self.is_file(path) {
            return;
        }
        self.seen.insert(path.to_path_buf());
        let Ok(source) = std::fs::read(path) else {
            self.failed = true;
            return;
        };
        let Some(text) = crate::source_text(&source) else {
            self.failed = true;
            return;
        };
        let Some(parsed) = crate::parse(text) else {
            self.failed = true;
            return;
        };
        let lines = guards::Lines::new(text);
        let names: Names = Rc::new(RefCell::new(HashMap::new()));
        let origins: Origins = Rc::new(RefCell::new(IndexMap::new()));
        self.names.insert(module.to_string(), names.clone());
        self.origins.insert(module.to_string(), origins.clone());
        let tests_names = crate::name_test(&source);
        let mut file = File {
            module: module.to_string(),
            package: package.to_string(),
            names,
            origins,
            contributions: vec![],
            classes: IndexMap::new(),
            tests_names,
        };
        self.walk(parsed.suite(), &mut file);
        if self.failed {
            return;
        }
        let referenced: Vec<String> = crate::referenced_body(parsed.suite()).into_iter().collect();
        let guards = guards::model_guards(parsed.suite(), &lines);
        let classes = self.exported_classes(&file);
        self.scans.push(ModuleScan {
            module: module.to_string(),
            path: path.to_string_lossy().into_owned(),
            contributions: std::mem::take(&mut file.contributions),
            referenced,
            guards,
            annotations: crate::annotation_aliases(&source),
            classes,
        });
    }

    fn exported_classes(&self, file: &File) -> Vec<FieldClass> {
        let mut exported: IndexMap<String, FieldClass> = file.classes.clone();
        let own = format!("{}.", self.prefix);
        for (name, origin) in file.origins.borrow().iter() {
            if exported.contains_key(name) {
                continue;
            }
            let other = origin.starts_with("odoo.addons.") && !origin.starts_with(&own);
            if self.field_classes.contains_key(origin) || other {
                exported.insert(name.clone(), FieldClass { name: name.clone(), bases: vec![origin.clone()], converts: false, arguments: false, passes: vec![] });
            }
        }
        exported.into_values().collect()
    }

    fn walk(&mut self, body: &[Stmt], file: &mut File) {
        for node in body {
            if self.failed {
                return;
            }
            match node {
                Stmt::ClassDef(c) => self.add_class(c, file),
                Stmt::ImportFrom(i) => self.import_from(i, file),
                Stmt::Import(i) => {
                    for alias in &i.names {
                        let dotted: Vec<String> = alias.name.as_str().split('.').map(str::to_string).collect();
                        self.import(&dotted);
                        let mut origins = file.origins.borrow_mut();
                        match &alias.asname {
                            Some(asname) => {
                                origins.insert(asname.as_str().to_string(), alias.name.as_str().to_string());
                            }
                            None => {
                                let top = alias.name.as_str().split('.').next().unwrap().to_string();
                                origins.insert(top.clone(), top);
                            }
                        }
                    }
                }
                Stmt::FunctionDef(f) if !f.is_async => {
                    let found = self.factory_class(f, file);
                    self.describe(file, f.name.as_str(), found);
                }
                Stmt::Assign(_) | Stmt::AnnAssign(_) | Stmt::AugAssign(_) => {
                    {
                        let mut names = file.names.borrow_mut();
                        assign(node, &mut names);
                    }
                    if let Stmt::Assign(a) = node {
                        self.bind_assignment(a, file);
                    }
                }
                Stmt::If(i) => {
                    // executed_blocks: [body, orelse] (or [orelse] under TYPE_CHECKING);
                    // an elif chain is Python's nested If: walked like one.
                    let tc = match &*i.test {
                        Expr::Name(n) => n.id.as_str() == "TYPE_CHECKING",
                        Expr::Attribute(a) => a.attr.as_str() == "TYPE_CHECKING",
                        _ => false,
                    };
                    if !tc {
                        self.walk(&i.body, file);
                    }
                    self.walk_clauses(i, 0, file);
                }
                other => {
                    for block in executed_blocks(other) {
                        self.walk(block, file);
                    }
                }
            }
        }
    }

    /// Python's `orelse` of an `If` from clause `n` on, walked as a block.
    fn walk_clauses(&mut self, i: &ruff_python_ast::StmtIf, n: usize, file: &mut File) {
        let Some(clause) = i.elif_else_clauses.get(n) else { return };
        match &clause.test {
            None => self.walk(&clause.body, file),
            Some(test) => {
                let tc = match test {
                    Expr::Name(x) => x.id.as_str() == "TYPE_CHECKING",
                    Expr::Attribute(a) => a.attr.as_str() == "TYPE_CHECKING",
                    _ => false,
                };
                if !tc {
                    self.walk(&clause.body, file);
                }
                self.walk_clauses(i, n + 1, file);
            }
        }
    }

    fn inner_parts(&self, dotted: &[String]) -> Option<Vec<String>> {
        for root in &self.roots {
            let parts: Vec<&str> = root.split('.').collect();
            if dotted.len() >= parts.len() && dotted.iter().zip(&parts).all(|(a, b)| a == b) {
                return Some(dotted[parts.len()..].to_vec());
            }
        }
        None
    }

    fn import(&mut self, dotted: &[String]) {
        if self.one_file {
            return;
        }
        let Some(parts) = self.inner_parts(dotted) else { return };
        let mut module = self.prefix.clone();
        let mut base = self.path.clone();
        for part in parts {
            module = format!("{module}.{part}");
            base = base.join(&part);
            self.visit_module(&module.clone(), &base.clone());
        }
    }

    fn import_from(&mut self, node: &ruff_python_ast::StmtImportFrom, file: &mut File) {
        let level = node.level as usize;
        let package: Vec<String> = if level > 0 { file.package.split('.').map(str::to_string).collect() } else { vec![] };
        // Python's `package[: len(package) - level + 1]`, a negative end too
        let mut dotted: Vec<String> = if level > 0 {
            let end = package.len() as isize - level as isize + 1;
            let end = if end >= 0 { (end as usize).min(package.len()) } else { (package.len() as isize + end).max(0) as usize };
            package[..end].to_vec()
        } else {
            vec![]
        };
        if let Some(m) = &node.module {
            dotted.extend(m.as_str().split('.').map(str::to_string));
        }
        let Some(parts) = self.inner_parts(&dotted) else {
            let mut origins = file.origins.borrow_mut();
            for alias in &node.names {
                if alias.name.as_str() != "*" {
                    let mut full = dotted.clone();
                    full.push(alias.name.as_str().to_string());
                    origins.insert(alias.asname.as_ref().unwrap_or(&alias.name).as_str().to_string(), full.join("."));
                }
            }
            return;
        };
        self.import(&dotted);
        let mut module_parts = vec![self.prefix.clone()];
        module_parts.extend(parts);
        let module = module_parts.join(".");
        // read once, before the names are imported (Python's `self._names.get(module, {})`)
        let source = self.names.get(&module).cloned();
        let module_origins = self.origins.get(&module).cloned();
        for alias in &node.names {
            let mut sub = dotted.clone();
            sub.push(alias.name.as_str().to_string());
            self.import(&sub);
            let name = alias.asname.as_ref().unwrap_or(&alias.name).as_str().to_string();
            if let Some(source) = &source {
                let value = source.borrow().get(alias.name.as_str()).cloned();
                if let Some(value) = value {
                    file.names.borrow_mut().insert(name.clone(), value);
                }
            }
            if alias.name.as_str() != "*" {
                let bound = module_origins.as_ref().and_then(|o| o.borrow().get(alias.name.as_str()).cloned());
                let origin = bound.unwrap_or_else(|| format!("{module}.{}", alias.name.as_str()));
                file.origins.borrow_mut().insert(name, origin);
            }
        }
    }

    fn origin(&self, file: &File, node: &Expr) -> Option<String> {
        let origin = origin_of(node, &file.origins.borrow())?;
        let dotted: Vec<String> = origin.split('.').map(str::to_string).collect();
        match self.inner_parts(&dotted) {
            Some(parts) => {
                let mut all = vec![self.prefix.clone()];
                all.extend(parts);
                Some(all.join("."))
            }
            None => Some(origin),
        }
    }

    fn linkable(&self, origin: &str) -> bool {
        self.one_file || !origin.starts_with(&format!("{}.", self.prefix))
    }

    /// `scan_file_classes`: the model classes of one file read on its own; `None` if the
    /// file cannot be read or parsed here (Python decides).
    pub fn file_classes(&mut self, path: &Path) -> Option<Vec<Contribution>> {
        let source = std::fs::read(path).ok()?;
        let text = crate::source_text(&source)?;
        let parsed = crate::parse(text)?;
        let names: Names = Rc::new(RefCell::new(HashMap::new()));
        let origins: Origins = Rc::new(RefCell::new(IndexMap::new()));
        let mut file = File {
            module: self.prefix.clone(),
            package: self.prefix.clone(),
            names,
            origins,
            contributions: vec![],
            classes: IndexMap::new(),
            tests_names: crate::name_test(&source),
        };
        self.walk(parsed.suite(), &mut file);
        if self.failed {
            return None;
        }
        Some(file.contributions)
    }

    fn derived(&self, name: &str, named: Vec<Option<String>>, mut converts: bool, mut arguments: bool, passes: BTreeSet<String>) -> Option<FieldClass> {
        let mut bases: IndexMap<String, ()> = IndexMap::new();
        let mut passed = passes;
        for base in named.into_iter().flatten() {
            if let Some(inner) = self.field_classes.get(&base) {
                for b in &inner.bases {
                    bases.insert(b.clone(), ());
                }
                converts |= inner.converts;
                arguments |= inner.arguments;
                passed.extend(inner.passes.iter().cloned());
            } else if odoo_class(&base).is_some() || self.linkable(&base) {
                bases.insert(base, ());
            }
        }
        if bases.is_empty() {
            return None;
        }
        Some(FieldClass { name: name.to_string(), bases: bases.into_keys().collect(), converts, arguments, passes: passed.into_iter().collect() })
    }

    fn factory_class(&self, f: &StmtFunctionDef, file: &File) -> Option<FieldClass> {
        let returned = returns_of(&f.body);
        let calls: Vec<&ExprCall> = returned.iter().filter_map(|v| if let Some(Expr::Call(c)) = v { Some(c) } else { None }).collect();
        if calls.is_empty() || calls.len() != returned.len() {
            return None;
        }
        let called: Vec<Option<String>> = calls.iter().map(|c| self.origin(file, &c.func)).collect();
        if called.iter().any(Option::is_none) {
            return None;
        }
        let passes: BTreeSet<String> = calls.iter().flat_map(|c| passed_names(c, !c.arguments.args.is_empty())).collect();
        self.derived(f.name.as_str(), called, false, true, passes)
    }

    fn class_bases(&self, node: &StmtClassDef, file: &File) -> Option<FieldClass> {
        let methods: HashSet<&str> = node.body.iter().filter_map(|s| if let Stmt::FunctionDef(f) = s { (!f.is_async).then(|| f.name.as_str()) } else { None }).collect();
        let bases: Vec<Option<String>> = node.arguments.as_ref().map(|a| a.args.iter().map(|b| self.origin(file, b)).collect()).unwrap_or_default();
        self.derived(
            node.name.as_str(),
            bases,
            methods.iter().any(|m| m.starts_with("convert_to_")),
            methods.contains("__init__"),
            BTreeSet::new(),
        )
    }

    fn describe(&mut self, file: &mut File, name: &str, found: Option<FieldClass>) {
        let fullname = format!("{}.{}", file.module, name);
        file.origins.borrow_mut().insert(name.to_string(), fullname.clone());
        if let Some(found) = found {
            self.field_classes.insert(fullname, found.clone());
            file.classes.insert(name.to_string(), found);
        }
    }

    fn bind_assignment(&mut self, node: &ruff_python_ast::StmtAssign, file: &mut File) {
        let named = if matches!(&*node.value, Expr::Name(_) | Expr::Attribute(_)) { self.origin(file, &node.value) } else { None };
        for target in &node.targets {
            let Expr::Name(t) = target else { continue };
            let made = self.partial_class(t.id.as_str(), &node.value, file);
            if made.is_some() {
                self.describe(file, t.id.as_str(), made);
            } else if let Some(named) = &named {
                file.origins.borrow_mut().insert(t.id.as_str().to_string(), named.clone());
            } else {
                file.origins.borrow_mut().shift_remove(t.id.as_str());
            }
        }
    }

    fn partial_class(&self, name: &str, value: &Expr, file: &File) -> Option<FieldClass> {
        let Expr::Call(c) = value else { return None };
        if c.arguments.args.is_empty() || self.origin(file, &c.func).as_deref() != Some(PARTIAL) {
            return None;
        }
        let passes: BTreeSet<String> = passed_names(c, c.arguments.args.len() > 1).into_iter().collect();
        self.derived(name, vec![self.origin(file, &c.arguments.args[0])], false, true, passes)
    }

    fn field_class_of(&self, file: &File, call: &ExprCall) -> Option<ClassOf> {
        let Some(origin) = self.origin(file, &call.func) else {
            if let Expr::Attribute(a) = &*call.func {
                if matches!(&*a.value, Expr::Name(n) if n.id.as_str() == "fields") && a.attr.as_str().chars().next().is_some_and(|c| c.is_uppercase()) {
                    let n = a.attr.as_str().to_string();
                    return Some(ClassOf::Kind(FieldKind { name: n.clone(), base: n, converts: false, arguments: false, passes: BTreeSet::new() }));
                }
            }
            return None;
        };
        if let Some(kind) = field_kind(&origin, &self.field_classes, &BTreeSet::new()) {
            return Some(ClassOf::Kind(kind));
        }
        (self.field_classes.contains_key(&origin) || self.linkable(&origin)).then_some(ClassOf::Name(origin))
    }

    fn add_class(&mut self, node: &StmtClassDef, file: &mut File) {
        let attrs = {
            let names = file.names.borrow();
            class_attributes(node, &*names)
        };
        file.names.borrow_mut().insert(node.name.as_str().to_string(), V::Unknown);
        let model = model_name(node.name.as_str(), &attrs);
        let fullname = format!("{}.{}", file.module, node.name.as_str());
        let register = attrs.get("_register").map(V::truthy).unwrap_or(true);
        let Some(model) = model.filter(|_| register) else {
            if file.origins.borrow().get(node.name.as_str()) == Some(&fullname) {
                return;
            }
            let found = self.class_bases(node, file);
            self.describe(file, node.name.as_str(), found);
            return;
        };
        file.origins.borrow_mut().shift_remove(node.name.as_str());
        if self.classes.contains(&fullname) {
            return;
        }
        self.classes.insert(fullname);
        let methods = class_bodies::defined_methods(node);
        let inherits = attrs.get("_inherits").cloned();
        let a = assigned::assigned_values(node);
        let fields = {
            let names = file.names.borrow();
            let scope = Chain { first: &attrs, second: &*names };
            let method_set: HashSet<String> = methods.iter().cloned().collect();
            self.declared_fields(node, &scope, &method_set, file)
        };
        let abstract_ = match attrs.get("_abstract") {
            Some(V::Bool(b)) => Some(*b),
            _ => {
                let kinds: HashSet<Option<&str>> = node.arguments.as_ref().map(|a| a.args.iter().map(callee_name).collect()).unwrap_or_default();
                if kinds.contains(&Some("Model")) || kinds.contains(&Some("TransientModel")) {
                    Some(false)
                } else if kinds.contains(&Some("AbstractModel")) {
                    Some(true)
                } else {
                    None
                }
            }
        };
        let log_access = match attrs.get("_log_access").or_else(|| attrs.get("_auto")) {
            Some(V::Bool(b)) => Some(*b),
            _ => None,
        };
        let parent_store = match attrs.get("_parent_store") {
            Some(V::Bool(b)) => Some(*b),
            _ => None,
        };
        let mut delegates: IndexMap<String, ()> = IndexMap::new();
        if let Some(V::Dict(d)) = &inherits {
            for k in d.keys() {
                delegates.insert(k.clone(), ());
            }
        }
        for statement in &node.body {
            let value = match statement {
                Stmt::Assign(a) => Some(&*a.value),
                Stmt::AnnAssign(a) => a.value.as_deref(),
                _ => None,
            };
            if let Some(Expr::Call(c)) = value {
                let delegate = callee_name(&c.func) == Some("Many2one")
                    && c.arguments.keywords.iter().any(|k| k.arg.as_ref().is_some_and(|a| a.as_str() == "delegate") && matches!(&k.value, Expr::BooleanLiteral(b) if b.value));
                if delegate {
                    if let Some(cm) = comodel_argument(c).filter(|x| !x.is_empty()) {
                        delegates.insert(cm, ());
                    }
                }
            }
        }
        let inherits_pairs: Vec<(String, String)> = match &inherits {
            Some(V::Dict(d)) => d.iter().filter_map(|(k, v)| v.as_str().map(|v| (k.clone(), v.to_string()))).collect(),
            _ => vec![],
        };
        let parents = match attrs.get("_inherit") {
            Some(V::Str(s)) => vec![s.clone()],
            Some(V::List(x)) | Some(V::Tuple(x)) => x.iter().filter_map(|i| i.as_str().map(str::to_string)).collect(),
            _ => vec![],
        };
        file.contributions.push(Contribution {
            addon: self.name.clone(),
            module: file.module.clone(),
            class_name: node.name.as_str().to_string(),
            model,
            parents,
            position: (self.load_key.0, self.load_key.1.clone(), self.classes.len() as i64),
            comodels: class_bodies::declared_comodels(node),
            fields,
            methods,
            delegates: delegates.into_keys().collect(),
            inherits: inherits_pairs,
            log_access,
            parent_store,
            abstract_,
            value_keys: class_bodies::read_value_keys(node),
            dispatches: if file.tests_names { class_bodies::record_dispatches(node) } else { vec![] },
            passed: class_bodies::passed_containers(node),
            assigned: a.known,
            assigned_any: a.unknown,
            assigned_through: a.through,
            filled: a.filled,
            super_returns: super_returns::super_returns(node),
        });
    }

    fn declared_fields(&self, node: &StmtClassDef, scope: &dyn Scope, methods: &HashSet<String>, file: &File) -> Vec<FieldInfo> {
        let mut found: IndexMap<String, FieldInfo> = IndexMap::new();
        for statement in &node.body {
            let (target, value) = match statement {
                Stmt::Assign(a) if a.targets.len() == 1 => (&a.targets[0], Some(&*a.value)),
                Stmt::AnnAssign(a) => (&*a.target, a.value.as_deref()),
                _ => continue,
            };
            let (Expr::Name(target), Some(Expr::Call(call))) = (target, value) else { continue };
            if let Some(kind) = self.field_class_of(file, call) {
                let name = target.id.as_str().to_string();
                found.shift_remove(&name);
                let info = field_info(&name, call, &kind, scope, methods);
                found.insert(name, info);
            }
        }
        found.into_values().collect()
    }
}

/// The `return` values of a function body, in the order Python's stack walk finds them.
fn returns_of(body: &[Stmt]) -> Vec<Option<&Expr>> {
    enum It<'a> {
        S(&'a Stmt),
        Elif(&'a ruff_python_ast::StmtIf, usize),
        H(&'a ExceptHandler),
        C(&'a ruff_python_ast::MatchCase),
    }
    let mut out = vec![];
    let mut pending: Vec<It> = body.iter().map(It::S).collect();
    while let Some(item) = pending.pop() {
        match item {
            It::S(Stmt::Return(r)) => out.push(r.value.as_deref()),
            It::S(Stmt::FunctionDef(_) | Stmt::ClassDef(_)) => {}
            It::S(Stmt::If(i)) => {
                pending.extend(i.body.iter().map(It::S));
                match i.elif_else_clauses.first() {
                    Some(c) if c.test.is_some() => pending.push(It::Elif(i, 0)),
                    Some(c) => pending.extend(c.body.iter().map(It::S)),
                    None => {}
                }
            }
            It::Elif(i, n) => {
                pending.extend(i.elif_else_clauses[n].body.iter().map(It::S));
                match i.elif_else_clauses.get(n + 1) {
                    Some(c) if c.test.is_some() => pending.push(It::Elif(i, n + 1)),
                    Some(c) => pending.extend(c.body.iter().map(It::S)),
                    None => {}
                }
            }
            It::S(Stmt::For(f)) => {
                pending.extend(f.body.iter().map(It::S));
                pending.extend(f.orelse.iter().map(It::S));
            }
            It::S(Stmt::While(w)) => {
                pending.extend(w.body.iter().map(It::S));
                pending.extend(w.orelse.iter().map(It::S));
            }
            It::S(Stmt::With(w)) => pending.extend(w.body.iter().map(It::S)),
            It::S(Stmt::Try(t)) => {
                pending.extend(t.body.iter().map(It::S));
                pending.extend(t.handlers.iter().map(It::H));
                pending.extend(t.orelse.iter().map(It::S));
                pending.extend(t.finalbody.iter().map(It::S));
            }
            It::H(ExceptHandler::ExceptHandler(h)) => pending.extend(h.body.iter().map(It::S)),
            It::S(Stmt::Match(m)) => pending.extend(m.cases.iter().map(It::C)),
            It::C(c) => pending.extend(c.body.iter().map(It::S)),
            It::S(_) => {}
        }
    }
    out
}

fn assign(statement: &Stmt, scope: &mut HashMap<String, V>) {
    let (targets, value): (Vec<&Expr>, V) = match statement {
        Stmt::Assign(a) => (a.targets.iter().collect(), evaluate(&a.value, &*scope)),
        Stmt::AnnAssign(a) if a.value.is_some() => (vec![&a.target], evaluate(a.value.as_deref().unwrap(), &*scope)),
        Stmt::AnnAssign(a) => (vec![&a.target], V::Unknown),
        Stmt::AugAssign(a) => (vec![&a.target], V::Unknown),
        _ => return,
    };
    for target in targets {
        match target {
            Expr::Name(n) => {
                scope.insert(n.id.as_str().to_string(), value.clone());
            }
            Expr::Tuple(t) => t.elts.iter().for_each(|e| if let Expr::Name(n) = e { scope.insert(n.id.as_str().to_string(), V::Unknown); }),
            Expr::List(t) => t.elts.iter().for_each(|e| if let Expr::Name(n) = e { scope.insert(n.id.as_str().to_string(), V::Unknown); }),
            _ => {}
        }
    }
}

/// A class body's assignments evaluated in order: its own names first, then the module's.
struct ClassScope<'m> {
    attrs: IndexMap<String, V>,
    module: &'m HashMap<String, V>,
}
impl Scope for ClassScope<'_> {
    fn get(&self, name: &str) -> Option<V> {
        self.attrs.get(name).cloned().or_else(|| self.module.get(name).cloned())
    }
}

fn class_attributes(node: &StmtClassDef, module: &HashMap<String, V>) -> IndexMap<String, V> {
    let mut scope = ClassScope { attrs: IndexMap::new(), module };
    for statement in &node.body {
        let (targets, value): (Vec<&Expr>, V) = match statement {
            Stmt::Assign(a) => (a.targets.iter().collect(), evaluate(&a.value, &scope)),
            Stmt::AnnAssign(a) if a.value.is_some() => (vec![&a.target], evaluate(a.value.as_deref().unwrap(), &scope)),
            Stmt::AnnAssign(a) => (vec![&a.target], V::Unknown),
            Stmt::AugAssign(a) => (vec![&a.target], V::Unknown),
            _ => continue,
        };
        for target in targets {
            match target {
                Expr::Name(n) => {
                    scope.attrs.insert(n.id.as_str().to_string(), value.clone());
                }
                Expr::Tuple(t) => t.elts.iter().for_each(|e| if let Expr::Name(n) = e { scope.attrs.insert(n.id.as_str().to_string(), V::Unknown); }),
                Expr::List(t) => t.elts.iter().for_each(|e| if let Expr::Name(n) = e { scope.attrs.insert(n.id.as_str().to_string(), V::Unknown); }),
                _ => {}
            }
        }
    }
    scope.attrs
}

fn model_name(class_name: &str, attrs: &IndexMap<String, V>) -> Option<String> {
    if let Some(name) = attrs.get("_name") {
        return name.as_str().map(str::to_string);
    }
    match attrs.get("_inherit").unwrap_or(&V::Unknown) {
        V::Str(s) => Some(s.clone()),
        V::List(x) | V::Tuple(x) => {
            if x.len() == 1 {
                x[0].as_str().map(str::to_string)
            } else {
                Some(class_name.to_string())
            }
        }
        _ => None,
    }
}

fn comodel_argument(call: &ExprCall) -> Option<String> {
    let text = |e: &Expr| if let Expr::StringLiteral(s) = e { Some(s.value.to_str().to_string()) } else { None };
    match call.arguments.args.first() {
        Some(first) => text(first),
        None => call.arguments.keywords.iter().find(|k| k.arg.as_ref().is_some_and(|a| a.as_str() == "comodel_name")).and_then(|k| text(&k.value)),
    }
}

fn unread_positional(field_type: &str) -> BTreeSet<String> {
    positional_parameters(field_type).unwrap_or(&[]).iter().filter_map(|p| positional_attribute(p)).map(str::to_string).collect()
}

fn field_arguments<'a>(field_type: Option<&str>, call: &'a ExprCall) -> IndexMap<String, &'a Expr> {
    let mut arguments = IndexMap::new();
    let parameters: &[&str] = match field_type {
        Some(t) => positional_parameters(t).unwrap_or(&["string"]),
        None => &[],
    };
    for (parameter, node) in parameters.iter().zip(call.arguments.args.iter()) {
        if matches!(node, Expr::Starred(_)) {
            break;
        }
        arguments.insert(parameter.to_string(), node);
    }
    for keyword in call.arguments.keywords.iter() {
        if let Some(arg) = &keyword.arg {
            arguments.insert(arg.as_str().to_string(), &keyword.value);
        }
    }
    arguments
}

fn type_name(value: &V) -> Option<&'static str> {
    match value {
        V::Str(s) if !s.is_empty() => Some("str"),
        V::Int(i) if *i != Some(0) => Some("int"),
        V::Float(f) if *f != 0.0 => Some("float"),
        _ => None,
    }
}

fn given_of(arguments: Option<&IndexMap<String, &Expr>>, call: &ExprCall, scope: &dyn Scope) -> Vec<String> {
    let Some(arguments) = arguments else { return vec![ANY_ARGUMENT.to_string()] };
    if call.arguments.keywords.iter().any(|k| k.arg.is_none()) || call.arguments.args.iter().any(|a| matches!(a, Expr::Starred(_))) {
        return vec![ANY_ARGUMENT.to_string()];
    }
    let mut found: Vec<String> = arguments
        .iter()
        .map(|(name, node)| {
            let scalar = matches!(node, Expr::Name(_) | Expr::BinOp(_) | Expr::FString(_)) || crate::value::constant(node).is_some();
            let value = if scalar { Some(evaluate(node, scope)) } else { None };
            match value.as_ref().and_then(type_name) {
                Some(t) => format!("{name}={t}"),
                None => name.clone(),
            }
        })
        .collect();
    found.sort();
    found
}

fn field_info(name: &str, call: &ExprCall, kind: &ClassOf, scope: &dyn Scope, methods: &HashSet<String>) -> FieldInfo {
    let mut dynamic: BTreeSet<String> = BTreeSet::new();
    let mut named: Option<IndexMap<String, &Expr>> = None;
    let (field_type, arguments) = match kind {
        ClassOf::Name(n) => (n.clone(), field_arguments(None, call)),
        ClassOf::Kind(k) if k.arguments => {
            if !call.arguments.args.is_empty() {
                dynamic.extend(unread_positional(&k.base));
            }
            (k.field_type(), field_arguments(None, call))
        }
        ClassOf::Kind(k) => {
            let arguments = field_arguments(Some(&k.base), call);
            let parameters = positional_parameters(&k.base).unwrap_or(&["string"]);
            if k.passes.is_empty() && call.arguments.args.len() <= parameters.len() {
                named = Some(arguments.clone());
            }
            (k.field_type(), arguments)
        }
    };
    let text = |key: &str, dynamic: &mut BTreeSet<String>| -> Option<String> {
        let node = arguments.get(key)?;
        match evaluate(node, scope) {
            V::Str(s) => Some(s),
            _ => {
                dynamic.insert(key.to_string());
                None
            }
        }
    };
    let method = |key: &str, dynamic: &mut BTreeSet<String>| -> Option<String> {
        let node = arguments.get(key)?;
        if let Expr::Name(n) = node {
            if methods.contains(n.id.as_str()) {
                return Some(n.id.as_str().to_string());
            }
        }
        text(key, dynamic)
    };
    let flag = |key: &str, dynamic: &mut BTreeSet<String>| -> Option<bool> {
        let node = arguments.get(key)?;
        match evaluate(node, scope) {
            V::Bool(b) => Some(b),
            _ => {
                dynamic.insert(key.to_string());
                None
            }
        }
    };
    let selection_keys = |key: &str, dynamic: &mut BTreeSet<String>| -> Option<Vec<String>> {
        let node = arguments.get(key)?;
        let keys = keys_of(&evaluate(node, scope));
        if keys.is_none() {
            dynamic.insert(key.to_string());
        }
        keys
    };
    // _selection
    let (selection, selection_method) = match arguments.get("selection") {
        None => (None, None),
        Some(Expr::Name(n)) if methods.contains(n.id.as_str()) => (None, Some(n.id.as_str().to_string())),
        Some(node) => match evaluate(node, scope) {
            V::Str(s) => (None, Some(s)),
            _ => (selection_keys("selection", &mut dynamic), None),
        },
    };
    if dynamic.remove("comodel_name") {
        dynamic.insert("comodel".to_string());
    }
    let positional = if let ClassOf::Name(_) = kind {
        let mut found = vec![];
        for node in call.arguments.args.iter() {
            if matches!(node, Expr::Starred(_)) {
                break;
            }
            let value = evaluate(node, scope);
            let m = match node {
                Expr::Name(n) if methods.contains(n.id.as_str()) => Some(n.id.as_str().to_string()),
                _ => None,
            };
            found.push((value.as_str().map(str::to_string), keys_of(&value), m));
        }
        Some(found)
    } else {
        None
    };
    let mut info = FieldInfo {
        unlinked: positional.is_some(),
        name: name.to_string(),
        field_type,
        ..Default::default()
    };
    info.comodel = text("comodel_name", &mut dynamic);
    info.inverse_name = text("inverse_name", &mut dynamic);
    info.selection = selection;
    info.selection_method = selection_method;
    info.selection_add = selection_keys("selection_add", &mut dynamic);
    info.related = text("related", &mut dynamic);
    info.compute = method("compute", &mut dynamic);
    info.inverse = method("inverse", &mut dynamic);
    info.search = method("search", &mut dynamic);
    info.readonly = flag("readonly", &mut dynamic);
    info.required = flag("required", &mut dynamic);
    info.store = flag("store", &mut dynamic);
    info.delegate = flag("delegate", &mut dynamic);
    info.compute_sql = arguments.contains_key("compute_sql").then_some(true);
    info.attachment = flag("attachment", &mut dynamic);
    info.given = given_of(named.as_ref(), call, scope);
    info.positional = positional.unwrap_or_default();
    if let ClassOf::Kind(k) = kind {
        if !k.passes.is_empty() {
            let mut found: BTreeSet<String> = BTreeSet::new();
            for parameter in &k.passes {
                if parameter == "*" {
                    found.extend(unread_positional(&k.base));
                } else if let Some(a) = argument_attribute(parameter) {
                    found.insert(a.to_string());
                }
            }
            let mut given: BTreeSet<String> = found.iter().filter(|n| info.is_set(n)).cloned().collect();
            if info.selection_method.is_some() {
                given.insert("selection".to_string());
            }
            dynamic.extend(found.difference(&given).cloned());
        }
    }
    info.dynamic = dynamic.into_iter().collect();
    info
}
