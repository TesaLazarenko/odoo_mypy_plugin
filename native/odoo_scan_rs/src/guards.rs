//! Port of `odoo_typing.guards.model_guards`: the lines where a model (a field, an
//! addon, who was asked for a field) is known to be there.
use ruff_python_ast::visitor::{walk_except_handler, walk_expr, walk_pattern, walk_stmt, Visitor};
use ruff_python_ast::{
    BoolOp, CmpOp, ExceptHandler, Expr, ExprBoolOp, ExprContext, ExprIf, Parameter, Pattern, Stmt,
    StmtAssign, StmtClassDef, StmtFunctionDef, StmtIf, UnaryOp,
};
use ruff_text_size::Ranged;
use std::collections::{BTreeSet, HashMap};

const FIELD: &str = "field:";
const ASKED: &str = "asked:";
const ADDON: &str = "addon:";
const REGISTRY_ATTRIBUTES: [&str; 3] = ["registry", "pool", "models"];
const ENV_RECORDS: [&str; 3] = ["user", "company", "companies"];

pub type Guard = (String, u32, u32);
type Names = BTreeSet<String>;
type Proves = (Names, Names);

/// Line numbers as Python counts them (1-based; `\n`, `\r\n` and `\r` end a line).
pub struct Lines {
    starts: Vec<usize>,
}

impl Lines {
    pub fn new(text: &str) -> Self {
        let bytes = text.as_bytes();
        let mut starts = vec![0];
        let mut i = 0;
        while i < bytes.len() {
            match bytes[i] {
                b'\n' => starts.push(i + 1),
                b'\r' => {
                    if i + 1 < bytes.len() && bytes[i + 1] == b'\n' {
                        i += 1;
                    }
                    starts.push(i + 1);
                }
                _ => {}
            }
            i += 1;
        }
        Lines { starts }
    }
    fn at(&self, offset: usize) -> u32 {
        (self.starts.partition_point(|&s| s <= offset)) as u32
    }
}

pub fn name_line(lines: &Lines, node: &impl Ranged) -> u32 {
    start_line(lines, node)
}
fn start_line(lines: &Lines, node: &impl Ranged) -> u32 {
    lines.at(node.start().to_usize())
}
fn end_line(lines: &Lines, node: &impl Ranged) -> u32 {
    lines.at(node.end().to_usize().saturating_sub(1))
}
/// `lineno` of a statement: a decorated function or class starts at its `def` / `class`.
fn stmt_line(lines: &Lines, stmt: &Stmt) -> u32 {
    match stmt {
        Stmt::FunctionDef(f) => start_line(lines, &f.name),
        Stmt::ClassDef(c) => start_line(lines, &c.name),
        other => start_line(lines, other),
    }
}

/// A statement as Python's tree has it: `elif` is an `If` in the `orelse` of the one before.
#[derive(Clone, Copy)]
enum S<'a> {
    Stmt(&'a Stmt),
    Elif(&'a StmtIf, usize),
}

enum Orelse<'a> {
    Empty,
    Block(&'a [Stmt]),
    Elif(&'a StmtIf, usize),
}

struct IfView<'a> {
    test: &'a Expr,
    body: &'a [Stmt],
    orelse: Orelse<'a>,
}

fn next_clause(s: &StmtIf, i: usize) -> Orelse<'_> {
    match s.elif_else_clauses.get(i) {
        None => Orelse::Empty,
        Some(clause) if clause.test.is_some() => Orelse::Elif(s, i),
        Some(clause) => Orelse::Block(&clause.body),
    }
}

fn if_view<'a>(s: S<'a>) -> Option<IfView<'a>> {
    match s {
        S::Stmt(Stmt::If(i)) => Some(IfView { test: &i.test, body: &i.body, orelse: next_clause(i, 0) }),
        S::Elif(i, n) => {
            let clause = &i.elif_else_clauses[n];
            Some(IfView { test: clause.test.as_ref().unwrap(), body: &clause.body, orelse: next_clause(i, n + 1) })
        }
        _ => None,
    }
}

fn orelse_items<'a>(orelse: &Orelse<'a>) -> Vec<S<'a>> {
    match orelse {
        Orelse::Empty => vec![],
        Orelse::Block(b) => b.iter().map(S::Stmt).collect(),
        Orelse::Elif(i, n) => vec![S::Elif(i, *n)],
    }
}

fn s_line(lines: &Lines, s: S<'_>) -> u32 {
    match s {
        S::Stmt(stmt) => stmt_line(lines, stmt),
        S::Elif(i, n) => start_line(lines, &i.elif_else_clauses[n]),
    }
}
fn s_end(lines: &Lines, s: S<'_>) -> u32 {
    match s {
        S::Stmt(stmt) => end_line(lines, stmt),
        S::Elif(i, _) => end_line(lines, i),
    }
}

fn ends(s: S<'_>) -> bool {
    match s {
        S::Stmt(Stmt::Return(_) | Stmt::Raise(_) | Stmt::Continue(_) | Stmt::Break(_)) => true,
        S::Stmt(Stmt::Expr(e)) => matches!(&*e.value, Expr::Call(c) if matches!(&*c.func, Expr::Attribute(a) if a.attr.as_str() == "skipTest")),
        _ => false,
    }
}

fn string(e: &Expr) -> Option<String> {
    match e {
        Expr::StringLiteral(s) => Some(s.value.to_str().to_string()),
        _ => None,
    }
}

fn is_env(e: &Expr) -> bool {
    match e {
        Expr::Attribute(a) => a.attr.as_str() == "env",
        Expr::Name(n) => n.id.as_str() == "env",
        _ => false,
    }
}

fn is_registry(mut e: &Expr) -> bool {
    while let Expr::Attribute(a) = e {
        if !REGISTRY_ATTRIBUTES.contains(&a.attr.as_str()) {
            break;
        }
        e = &a.value;
        if let Expr::Name(n) = e {
            if n.id.as_str() == "self" || n.id.as_str() == "cls" {
                return true;
            }
        }
    }
    is_env(e)
}

fn dotted(e: &Expr) -> Option<String> {
    match e {
        Expr::Name(n) => Some(n.id.as_str().to_string()),
        Expr::Attribute(a) => dotted(&a.value).map(|owner| format!("{owner}.{}", a.attr.as_str())),
        _ => None,
    }
}

fn contained_name(key: &Expr, container: &Expr) -> Option<String> {
    let name = string(key)?;
    if is_registry(container) {
        return Some(name);
    }
    match container {
        Expr::Attribute(a) if a.attr.as_str() == "_fields" => Some(format!("{FIELD}{name}")),
        Expr::Subscript(s) if is_registry(&s.value) => Some(format!("{FIELD}{name}")),
        Expr::Attribute(a) if ENV_RECORDS.contains(&a.attr.as_str()) && is_env(&a.value) => Some(format!("{FIELD}{name}")),
        _ => None,
    }
}

fn contained_names(key: &Expr, container: &Expr) -> Names {
    let mut names = Names::new();
    let Some(name) = contained_name(key, container) else { return names };
    if let Expr::Attribute(a) = container {
        if a.attr.as_str() == "_fields" {
            if let Some(records) = dotted(&a.value) {
                names.insert(format!("{ASKED}{records}:{}", name.strip_prefix(FIELD).unwrap_or(&name)));
            }
        }
    }
    names.insert(name);
    names
}

fn is_optional_ref(call: &ruff_python_ast::ExprCall) -> bool {
    matches!(&*call.func, Expr::Attribute(a) if a.attr.as_str() == "ref" && is_registry(&a.value))
        && !call.arguments.args.is_empty()
        && call.arguments.keywords.iter().any(|k| {
            k.arg.as_ref().is_some_and(|a| a.as_str() == "raise_if_not_found")
                && matches!(&k.value, Expr::BooleanLiteral(b) if !b.value)
        })
}

#[derive(Default, Clone)]
struct Known<'k> {
    flags: Option<&'k HashMap<String, Proves>>,
    helpers: Option<&'k HashMap<String, Proves>>,
}

fn test_models(test: &Expr, known: &Known<'_>) -> Proves {
    match test {
        Expr::UnaryOp(u) if u.op == UnaryOp::Not => {
            let (t, f) = test_models(&u.operand, known);
            return (f, t);
        }
        Expr::BoolOp(b) => {
            let parts: Vec<Proves> = b.values.iter().map(|v| test_models(v, known)).collect();
            return if b.op == BoolOp::And {
                (parts.into_iter().flat_map(|(t, _)| t).collect(), Names::new())
            } else {
                (Names::new(), parts.into_iter().flat_map(|(_, f)| f).collect())
            };
        }
        Expr::Compare(c) if c.ops.len() == 1 => {
            let names = contained_names(&c.left, &c.comparators[0]);
            if !names.is_empty() && c.ops[0] == CmpOp::In {
                return (names, Names::new());
            }
            if !names.is_empty() && c.ops[0] == CmpOp::NotIn {
                return (Names::new(), names);
            }
        }
        _ => {}
    }
    if let Expr::Call(call) = test {
        let args = &call.arguments.args;
        if let Expr::Attribute(func) = &*call.func {
            if func.attr.as_str() == "get" && !args.is_empty() {
                let mut names = contained_names(&args[0], &func.value);
                if args.len() > 1 || !call.arguments.keywords.is_empty() {
                    names.retain(|n| !n.starts_with(ASKED));
                }
                if !names.is_empty() {
                    return (names, Names::new());
                }
            }
        }
        if let Expr::Name(func) = &*call.func {
            if func.id.as_str() == "hasattr" && args.len() == 2 {
                if let Some(name) = string(&args[1]) {
                    return ([format!("{FIELD}{name}")].into(), Names::new());
                }
            }
        }
    }
    if let Expr::Name(n) = test {
        if let Some(p) = known.flags.and_then(|f| f.get(n.id.as_str())) {
            return p.clone();
        }
    }
    if let Expr::Call(call) = test {
        if call.arguments.args.is_empty() {
            if let Expr::Attribute(func) = &*call.func {
                if let Expr::Name(owner) = &*func.value {
                    if owner.id.as_str() == "self" || owner.id.as_str() == "cls" {
                        if let Some(p) = known.helpers.and_then(|h| h.get(func.attr.as_str())) {
                            return p.clone();
                        }
                    }
                }
            }
        }
        if is_optional_ref(call) {
            if let Some(name) = string(&call.arguments.args[0]) {
                if name.contains('.') {
                    let addon = name.split('.').next().unwrap();
                    return ([format!("{ADDON}{addon}")].into(), Names::new());
                }
            }
        }
    }
    (Names::new(), Names::new())
}

/// `walk_stmt`, but an `elif` test once (ruff's visitor visits it twice).
pub fn walk_stmt_once<'a, V: Visitor<'a> + ?Sized>(v: &mut V, stmt: &'a Stmt) {
    if let Stmt::If(i) = stmt {
        v.visit_expr(&i.test);
        v.visit_body(&i.body);
        for clause in &i.elif_else_clauses {
            if let Some(test) = &clause.test {
                v.visit_expr(test);
            }
            v.visit_body(&clause.body);
        }
    } else {
        walk_stmt(v, stmt);
    }
}

/// The nodes a guard can come from, collected in one walk.
#[derive(Default)]
struct Collected<'a> {
    if_exps: Vec<&'a ExprIf>,
    bool_ops: Vec<&'a ExprBoolOp>,
    assigns: Vec<&'a StmtAssign>,
    functions: Vec<&'a StmtFunctionDef>,
    classes: Vec<&'a StmtClassDef>,
    blocks: Vec<Vec<S<'a>>>,
}

impl<'a> Collected<'a> {
    fn block(&mut self, body: &'a [Stmt]) {
        if !body.is_empty() {
            self.blocks.push(body.iter().map(S::Stmt).collect());
        }
    }
    fn orelse(&mut self, orelse: Orelse<'a>) {
        let items = orelse_items(&orelse);
        if !items.is_empty() {
            self.blocks.push(items);
        }
    }
}

impl<'a> Visitor<'a> for Collected<'a> {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        match stmt {
            Stmt::FunctionDef(f) => {
                self.functions.push(f);
                self.block(&f.body);
            }
            Stmt::ClassDef(c) => {
                self.classes.push(c);
                self.block(&c.body);
            }
            Stmt::Assign(a) => self.assigns.push(a),
            Stmt::For(f) => {
                self.block(&f.body);
                self.block(&f.orelse);
            }
            Stmt::While(w) => {
                self.block(&w.body);
                self.block(&w.orelse);
            }
            Stmt::If(i) => {
                self.block(&i.body);
                self.orelse(next_clause(i, 0));
                for (n, clause) in i.elif_else_clauses.iter().enumerate() {
                    if clause.test.is_some() {
                        self.block(&clause.body);
                        self.orelse(next_clause(i, n + 1));
                    }
                }
            }
            Stmt::With(w) => self.block(&w.body),
            Stmt::Try(t) => {
                self.block(&t.body);
                self.block(&t.orelse);
                self.block(&t.finalbody);
            }
            Stmt::Match(m) => {
                for case in &m.cases {
                    self.block(&case.body);
                }
            }
            _ => {}
        }
        walk_stmt_once(self, stmt);
    }
    fn visit_except_handler(&mut self, handler: &'a ExceptHandler) {
        let ExceptHandler::ExceptHandler(h) = handler;
        self.block(&h.body);
        walk_except_handler(self, handler);
    }
    fn visit_expr(&mut self, expr: &'a Expr) {
        match expr {
            Expr::If(e) => self.if_exps.push(e),
            Expr::BoolOp(b) => self.bool_ops.push(b),
            _ => {}
        }
        walk_expr(self, expr);
    }
}

struct Scope {
    first: u32,
    last: u32,
    names: HashMap<String, Proves>,
}

fn innermost<'a, T>(nodes: &[&'a T], line: u32, span: impl Fn(&T) -> (u32, u32)) -> Option<&'a T> {
    let mut found: Option<(&'a T, u32)> = None;
    for &node in nodes {
        let (first, last) = span(node);
        if first <= line && line <= last && found.is_none_or(|(_, f)| first >= f) {
            found = Some((node, first));
        }
    }
    found.map(|(n, _)| n)
}

fn innermost_scope(scopes: &[Scope], line: u32) -> Option<&Scope> {
    let mut found: Option<&Scope> = None;
    for scope in scopes {
        if scope.first <= line && line <= scope.last && found.is_none_or(|f| scope.first >= f.first) {
            found = Some(scope);
        }
    }
    found
}

/// Counts the `Name` stores of a name (and finds other bindings of it) in a function.
struct Binds<'p> {
    name: &'p str,
    stores: usize,
    own: Vec<*const Parameter>,
    rebound: bool,
}

impl<'a> Visitor<'a> for Binds<'_> {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        walk_stmt_once(self, stmt);
    }
    fn visit_expr(&mut self, expr: &'a Expr) {
        if let Expr::Name(n) = expr {
            if n.id.as_str() == self.name && n.ctx == ExprContext::Store {
                self.stores += 1;
            }
        }
        walk_expr(self, expr);
    }
    fn visit_parameter(&mut self, parameter: &'a Parameter) {
        if parameter.name.as_str() == self.name && !self.own.contains(&(parameter as *const _)) {
            self.rebound = true;
        }
        ruff_python_ast::visitor::walk_parameter(self, parameter);
    }
    fn visit_pattern(&mut self, pattern: &'a Pattern) {
        let hit = match pattern {
            Pattern::MatchAs(p) => p.name.as_ref().is_some_and(|n| n.as_str() == self.name),
            Pattern::MatchStar(p) => p.name.as_ref().is_some_and(|n| n.as_str() == self.name),
            Pattern::MatchMapping(p) => p.rest.as_ref().is_some_and(|n| n.as_str() == self.name),
            _ => false,
        };
        self.rebound |= hit;
        walk_pattern(self, pattern);
    }
    fn visit_except_handler(&mut self, handler: &'a ExceptHandler) {
        let ExceptHandler::ExceptHandler(h) = handler;
        self.rebound |= h.name.as_ref().is_some_and(|n| n.as_str() == self.name);
        walk_except_handler(self, handler);
    }
    fn visit_alias(&mut self, alias: &'a ruff_python_ast::Alias) {
        let bound = match &alias.asname {
            Some(asname) => asname.as_str(),
            None => alias.name.as_str().split('.').next().unwrap(),
        };
        self.rebound |= bound == self.name;
    }
}

pub fn model_guards(body: &[Stmt], lines: &Lines) -> Vec<Guard> {
    let mut c = Collected::default();
    c.block(body);
    for stmt in body {
        c.visit_stmt(stmt);
    }
    let fspan = |f: &StmtFunctionDef| (start_line(lines, &f.name), end_line(lines, f));
    let cspan = |k: &StmtClassDef| (start_line(lines, &k.name), end_line(lines, k));

    // flags: a name a function assigns a guard test to, once
    let mut flags: Vec<(*const StmtFunctionDef, Scope)> = vec![];
    for a in &c.assigns {
        let [Expr::Name(target)] = &a.targets[..] else { continue };
        let proves = test_models(&a.value, &Known::default());
        if proves.0.is_empty() && proves.1.is_empty() {
            continue;
        }
        let Some(function) = innermost(&c.functions, start_line(lines, *a), fspan) else { continue };
        let mut counter = Binds { name: target.id.as_str(), stores: 0, own: vec![], rebound: false };
        walk_function(&mut counter, function);
        if counter.stores != 1 {
            continue;
        }
        let key = function as *const _;
        let position = match flags.iter().position(|(k, _)| *k == key) {
            Some(p) => p,
            None => {
                let (first, last) = fspan(function);
                flags.push((key, Scope { first, last, names: HashMap::new() }));
                flags.len() - 1
            }
        };
        flags[position].1.names.insert(target.id.as_str().to_string(), proves);
    }
    // helpers: a method whose body is `return <test>`
    let mut helpers: Vec<(*const StmtClassDef, Scope)> = vec![];
    for f in &c.functions {
        if f.parameters.args.len() > 1 || !f.parameters.posonlyargs.is_empty() {
            continue;
        }
        let mut body: &[Stmt] = &f.body;
        if let Some(Stmt::Expr(e)) = body.first() {
            if matches!(&*e.value, Expr::StringLiteral(_)) {
                body = &body[1..];
            }
        }
        let [Stmt::Return(r)] = body else { continue };
        let Some(value) = &r.value else { continue };
        let proves = test_models(value, &Known::default());
        let Some(owner) = innermost(&c.classes, start_line(lines, &f.name), cspan) else { continue };
        if proves.0.is_empty() && proves.1.is_empty() {
            continue;
        }
        let key = owner as *const _;
        let position = match helpers.iter().position(|(k, _)| *k == key) {
            Some(p) => p,
            None => {
                let (first, last) = cspan(owner);
                helpers.push((key, Scope { first, last, names: HashMap::new() }));
                helpers.len() - 1
            }
        };
        helpers[position].1.names.insert(f.name.as_str().to_string(), proves);
    }
    let flag_scopes: Vec<Scope> = flags.into_iter().map(|(_, s)| s).collect();
    let helper_scopes: Vec<Scope> = helpers.into_iter().map(|(_, s)| s).collect();
    let known_at = |line: u32| Known {
        flags: innermost_scope(&flag_scopes, line).map(|s| &s.names),
        helpers: innermost_scope(&helper_scopes, line).map(|s| &s.names),
    };
    let cover_exprs = |models: &Names, nodes: &[&Expr]| -> Vec<Guard> {
        if nodes.is_empty() {
            return vec![];
        }
        let first = start_line(lines, nodes[0]);
        let last = end_line(lines, *nodes.last().unwrap());
        models.iter().map(|m| (m.clone(), first, last)).collect()
    };
    let cover = |models: &Names, nodes: &[S<'_>]| -> Vec<Guard> {
        if nodes.is_empty() {
            return vec![];
        }
        let first = s_line(lines, nodes[0]);
        let last = s_end(lines, *nodes.last().unwrap());
        models.iter().map(|m| (m.clone(), first, last)).collect()
    };

    let mut guards: Vec<Guard> = vec![];
    for e in &c.if_exps {
        let known = known_at(start_line(lines, *e));
        let (t, f) = test_models(&e.test, &known);
        guards.extend(cover_exprs(&t, &[&e.body]));
        guards.extend(cover_exprs(&f, &[&e.orelse]));
    }
    for b in &c.bool_ops {
        let known = known_at(start_line(lines, *b));
        for (position, value) in b.values[..b.values.len() - 1].iter().enumerate() {
            let (t, f) = test_models(value, &known);
            let proven = if b.op == BoolOp::And { t } else { f };
            let rest: Vec<&Expr> = b.values[position + 1..].iter().collect();
            guards.extend(cover_exprs(&proven, &rest));
        }
    }
    guards.retain(|g| !g.0.starts_with(ASKED));
    for block in &c.blocks {
        let known = known_at(s_line(lines, block[0]));
        for (position, &statement) in block.iter().enumerate() {
            let Some(view) = if_view(statement) else { continue };
            let (t, f) = test_models(view.test, &known);
            let body: Vec<S<'_>> = view.body.iter().map(S::Stmt).collect();
            let orelse = orelse_items(&view.orelse);
            let mut found = cover(&t, &body);
            found.extend(cover(&f, &orelse));
            let rest = &block[position + 1..];
            if !rest.is_empty() && body.last().is_some_and(|&s| ends(s)) {
                found.extend(cover(&f, rest));
            }
            if !rest.is_empty() && orelse.last().is_some_and(|&s| ends(s)) {
                found.extend(cover(&t, rest));
            }
            let tested = end_line(lines, view.test);
            for guard in found {
                if (guard.1 > tested || !guard.0.starts_with(ASKED)) && still_asked(&guard, &c.functions, lines) {
                    guards.push(guard);
                }
            }
        }
    }
    guards
}

fn still_asked(guard: &Guard, functions: &[&StmtFunctionDef], lines: &Lines) -> bool {
    let Some(rest) = guard.0.strip_prefix(ASKED) else { return true };
    let root = rest.split(':').next().unwrap().split('.').next().unwrap();
    let outside: Vec<&StmtFunctionDef> =
        functions.iter().copied().filter(|f| start_line(lines, &f.name) < guard.1).collect();
    let Some(function) = innermost(&outside, guard.1, |f| (start_line(lines, &f.name), end_line(lines, f))) else {
        return false;
    };
    let p = &function.parameters;
    let own: Vec<&Parameter> =
        p.posonlyargs.iter().chain(&p.args).chain(&p.kwonlyargs).map(|a| &a.parameter).collect();
    if !own.iter().any(|a| a.name.as_str() == root) {
        return false;
    }
    let mut binds = Binds { name: root, stores: 0, own: own.iter().map(|a| *a as *const _).collect(), rebound: false };
    walk_function(&mut binds, function);
    binds.stores == 0 && !binds.rebound
}

/// Walk a function as `ast.walk` does: decorators, parameters, returns, body.
fn walk_function<'a, V: Visitor<'a>>(v: &mut V, f: &'a StmtFunctionDef) {
    for d in &f.decorator_list {
        v.visit_decorator(d);
    }
    if let Some(tp) = &f.type_params {
        v.visit_type_params(tp);
    }
    v.visit_parameters(&f.parameters);
    if let Some(r) = &f.returns {
        v.visit_annotation(r);
    }
    v.visit_body(&f.body);
}
