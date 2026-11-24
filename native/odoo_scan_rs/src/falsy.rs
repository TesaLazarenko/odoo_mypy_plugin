//! Port of `odoo_typing.falsy_guards.FalsyGuards`: the parameters an Odoo function
//! handles itself when they are empty, read from the function's own text.
use crate::parse;
use regex::Regex;
use ruff_python_ast::visitor::{walk_expr, walk_parameter, Visitor};
use ruff_python_ast::{
    BoolOp, CmpOp, ExceptHandler, Expr, ExprCall, ExprContext, InterpolatedStringElement, Int, Number, Parameter,
    Pattern, Stmt, StmtFunctionDef, StmtIf, TypeParam, UnaryOp,
};
use std::collections::{BTreeSet, HashMap};
use std::sync::{Arc, LazyLock};

static DEFINITION: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^(?P<indent>[ \t]*)(?:async\s+)?def\s+(?P<name>\w+)\s*\(").unwrap());
static CLASS: LazyLock<Regex> = LazyLock::new(|| Regex::new(r"^class\s+(?P<name>\w+)\b").unwrap());
static ALIAS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[ \t]+(?P<name>\w+)\s*=\s*(?P<target>\w+)\s*(?:#.*)?$").unwrap());
const MAX_ALIASES: usize = 4;

#[derive(Clone, Default)]
pub struct Guard {
    pub falsy: BTreeSet<String>,
    pub booleans: BTreeSet<String>,
    pub none_only_for: BTreeSet<String>,
    pub callable_first: bool,
}

struct Index {
    lines: Vec<String>,
    definitions: HashMap<(String, String), usize>,
    aliases: HashMap<(String, String), String>,
}

/// `str.isspace` of one character (Rust's whitespace leaves out U+001C..U+001F).
fn py_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}
fn strip(s: &str) -> &str {
    s.trim_matches(py_space)
}
fn lstrip(s: &str) -> &str {
    s.trim_start_matches(py_space)
}
fn indent_of(s: &str) -> usize {
    s.chars().count() - lstrip(s).chars().count()
}

/// `path.read_text(encoding="utf-8").splitlines()`.
fn split_lines(text: &str) -> Vec<String> {
    let text = text.replace("\r\n", "\n").replace('\r', "\n");
    let mut lines = Vec::new();
    let mut current = String::new();
    for c in text.chars() {
        if matches!(c, '\n' | '\u{0b}' | '\u{0c}' | '\u{1c}' | '\u{1d}' | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}') {
            lines.push(std::mem::take(&mut current));
        } else {
            current.push(c);
        }
    }
    if !current.is_empty() {
        lines.push(current);
    }
    lines
}

fn read_index(path: &str) -> Option<Index> {
    let data = std::fs::read(path).ok()?;
    let text = std::str::from_utf8(&data).ok()?;
    let lines = split_lines(text);
    let mut definitions = HashMap::new();
    let mut aliases = HashMap::new();
    let mut owner = String::new();
    let mut member_indent: Option<String> = None;
    for (number, line) in lines.iter().enumerate() {
        if strip(line).is_empty() || lstrip(line).starts_with('#') {
            continue;
        }
        if !py_space(line.chars().next().unwrap()) {
            owner = CLASS.captures(line).map(|c| c["name"].to_string()).unwrap_or_default();
            member_indent = None;
        }
        if let Some(found) = DEFINITION.captures(line) {
            let indent = &found["indent"];
            let name = found["name"].to_string();
            if indent.is_empty() {
                definitions.entry((String::new(), name)).or_insert(number);
            } else if !owner.is_empty() && member_indent.as_deref().is_none_or(|m| m == indent) {
                member_indent = Some(indent.to_string());
                definitions.entry((owner.clone(), name)).or_insert(number);
            }
            continue;
        }
        if owner.is_empty() {
            continue;
        }
        if let Some(found) = ALIAS.captures(line) {
            if member_indent.as_deref().is_none_or(|m| line.starts_with(m)) {
                aliases.entry((owner.clone(), found["name"].to_string())).or_insert_with(|| found["target"].to_string());
            }
        }
    }
    Some(Index { lines, definitions, aliases })
}

/// `textwrap.dedent` (Python 3.12) of lines joined by `\n`.
fn dedent(lines: &[String]) -> String {
    let lines: Vec<&str> =
        lines.iter().map(|l| if !l.is_empty() && l.chars().all(|c| c == ' ' || c == '\t') { "" } else { l.as_str() }).collect();
    let mut margin: Option<&str> = None;
    for line in &lines {
        let Some(at) = line.find(|c: char| c != ' ' && c != '\t') else { continue };
        let indent = &line[..at];
        margin = Some(match margin {
            None => indent,
            Some(m) if indent.starts_with(m) => m,
            Some(m) if m.starts_with(indent) => indent,
            Some(m) => {
                let common = m.bytes().zip(indent.bytes()).take_while(|(x, y)| x == y).count();
                &m[..common]
            }
        });
    }
    match margin {
        Some(m) if !m.is_empty() => lines.iter().map(|l| l.strip_prefix(m).unwrap_or(l)).collect::<Vec<_>>().join("\n"),
        _ => lines.join("\n"),
    }
}

fn function_text(lines: &[String], start: usize) -> String {
    let indent = indent_of(&lines[start]);
    let mut end = start + 1;
    while end < lines.len() {
        let line = &lines[end];
        let stripped = strip(line);
        if !stripped.is_empty() && !stripped.starts_with('#') {
            if indent_of(line) <= indent && !stripped.starts_with(')') && !stripped.starts_with(']') {
                break;
            }
        }
        end += 1;
    }
    dedent(&lines[start..end])
}

enum Where<'a> {
    Index(usize),
    Keyword(&'a str),
}

fn parameter_at(text: &str, at: &Where<'_>) -> Option<String> {
    let parsed = parse(text)?;
    let Some(Stmt::FunctionDef(f)) = parsed.suite().first() else { return None };
    let p = &f.parameters;
    match at {
        Where::Index(i) => p.posonlyargs.iter().chain(&p.args).nth(*i).map(|a| a.parameter.name.to_string()),
        Where::Keyword(k) => {
            p.args.iter().chain(&p.kwonlyargs).any(|a| a.parameter.name.as_str() == *k).then(|| k.to_string())
        }
    }
}

#[derive(Default)]
pub struct State {
    indexes: HashMap<String, Option<Arc<Index>>>,
    guards: HashMap<(String, String, String), Guard>,
}

impl State {
    fn index(&mut self, path: &str) -> Option<Arc<Index>> {
        if let Some(found) = self.indexes.get(path) {
            return found.clone();
        }
        let found = read_index(path).map(Arc::new);
        self.indexes.insert(path.to_string(), found.clone());
        found
    }

    pub fn of(&mut self, path: &str, owner: &str, name: &str) -> Guard {
        let key = (path.to_string(), owner.to_string(), name.to_string());
        if let Some(found) = self.guards.get(&key) {
            return found.clone();
        }
        self.guards.insert(key.clone(), Guard::default()); // a function that hands a parameter back to itself
        let found = self.read(path, owner, name);
        self.guards.insert(key, found.clone());
        found
    }

    fn read(&mut self, path: &str, owner: &str, name: &str) -> Guard {
        let Some(index) = self.index(path) else { return Guard::default() };
        let mut name = name.to_string();
        for _ in 0..MAX_ALIASES {
            if let Some(&start) = index.definitions.get(&(owner.to_string(), name.clone())) {
                return guard(&function_text(&index.lines, start), &mut Passes { state: self, path });
            }
            match index.aliases.get(&(owner.to_string(), name.clone())) {
                None => return Guard::default(),
                Some(target) => name = target.clone(),
            }
        }
        Guard::default()
    }
}

struct Passes<'s> {
    state: &'s mut State,
    path: &'s str,
}

impl Passes<'_> {
    fn passes(&mut self, call: &ExprCall, at: Where<'_>) -> bool {
        let Expr::Name(func) = &*call.func else { return false };
        let Some(index) = self.state.index(self.path) else { return false };
        let Some(&start) = index.definitions.get(&(String::new(), func.id.to_string())) else { return false };
        let Some(parameter) = parameter_at(&function_text(&index.lines, start), &at) else { return false };
        self.state.of(self.path, "", func.id.as_str()).falsy.contains(&parameter)
    }
}

fn is_zero(e: &Expr) -> bool {
    match e {
        Expr::NumberLiteral(n) => match &n.value {
            Number::Int(i) => *i == Int::ZERO,
            Number::Float(f) => *f == 0.0,
            Number::Complex { real, imag } => *real == 0.0 && *imag == 0.0,
        },
        Expr::BooleanLiteral(b) => !b.value,
        _ => false,
    }
}

/// `return`, `return None`, `return False` (and what equals them: `0`).
fn is_empty_value(e: Option<&Expr>) -> bool {
    match e {
        None => true,
        Some(Expr::NoneLiteral(_)) => true,
        Some(other) => is_zero(other),
    }
}

fn is_constant(e: &Expr) -> bool {
    matches!(
        e,
        Expr::StringLiteral(_)
            | Expr::BytesLiteral(_)
            | Expr::NumberLiteral(_)
            | Expr::BooleanLiteral(_)
            | Expr::NoneLiteral(_)
            | Expr::EllipsisLiteral(_)
    )
}

fn named<'e>(e: &'e Expr, id: &str) -> bool {
    matches!(e, Expr::Name(n) if n.id.as_str() == id)
}

fn parameter(e: &Expr, parameters: &BTreeSet<String>) -> Option<String> {
    match e {
        Expr::Name(n) if parameters.contains(n.id.as_str()) => Some(n.id.to_string()),
        _ => None,
    }
}

fn is_bool_test(test: &Expr) -> bool {
    if let Expr::Call(c) = test {
        if named(&c.func, "isinstance") && c.arguments.args.len() == 2 && named(&c.arguments.args[1], "bool") {
            return true;
        }
    }
    let Expr::Compare(c) = test else { return false };
    c.ops.len() == 1
        && c.ops[0] == CmpOp::Is
        && matches!(&*c.left, Expr::Call(l) if named(&l.func, "type") && l.arguments.args.len() == 1)
        && named(&c.comparators[0], "bool")
}

#[derive(PartialEq)]
enum Kind {
    Falsy,
    Bool,
}

fn leading_guard<'a>(statement: &'a Stmt, parameters: &BTreeSet<String>) -> Option<(String, Kind, Option<&'a Expr>)> {
    let Stmt::If(s) = statement else { return None };
    if !s.elif_else_clauses.is_empty() || s.body.len() != 1 {
        return None;
    }
    let Stmt::Return(r) = &s.body[0] else { return None };
    let test = &*s.test;
    let returned = r.value.as_deref();
    if let Expr::UnaryOp(u) = test {
        if u.op == UnaryOp::Not {
            return parameter(&u.operand, parameters).map(|n| (n, Kind::Falsy, returned));
        }
    }
    if let Expr::Compare(c) = test {
        if c.ops.len() == 1 && c.ops[0] == CmpOp::Is && matches!(c.comparators[0], Expr::NoneLiteral(_)) {
            return parameter(&c.left, parameters).map(|n| (n, Kind::Falsy, returned));
        }
    }
    if is_bool_test(test) {
        let call_or_compare = match test {
            Expr::Call(c) => &c.arguments.args[0],
            other => other,
        };
        let mut subject = match call_or_compare {
            Expr::Compare(c) => &*c.left,
            other => other,
        };
        if let Expr::Call(c) = subject {
            subject = c.arguments.args.first().unwrap_or(subject);
        }
        return parameter(subject, parameters).map(|n| (n, Kind::Bool, returned));
    }
    None
}

fn returns<'a>(statement: &'a Stmt, found: &mut Vec<Option<&'a Expr>>) {
    use ruff_python_ast::statement_visitor::{walk_stmt, StatementVisitor};
    struct Returns<'a, 'f> {
        found: &'f mut Vec<Option<&'a Expr>>,
    }
    impl<'a> StatementVisitor<'a> for Returns<'a, '_> {
        fn visit_stmt(&mut self, stmt: &'a Stmt) {
            match stmt {
                Stmt::Return(r) => self.found.push(r.value.as_deref()),
                Stmt::FunctionDef(_) | Stmt::ClassDef(_) => {}
                other => walk_stmt(self, other),
            }
        }
    }
    Returns { found }.visit_stmt(statement);
}

struct CallableFirst<'s> {
    star: &'s str,
    found: bool,
}

impl<'a> Visitor<'a> for CallableFirst<'_> {
    fn visit_expr(&mut self, e: &'a Expr) {
        if let Expr::Call(c) = e {
            if named(&c.func, "callable") && c.arguments.args.len() == 1 {
                if let Expr::Subscript(s) = &c.arguments.args[0] {
                    if named(&s.value, self.star) && is_zero(&s.slice) {
                        self.found = true;
                    }
                }
            }
        }
        walk_expr(self, e);
    }
}

fn guard(text: &str, passes: &mut Passes<'_>) -> Guard {
    let Some(parsed) = parse(text) else { return Guard::default() };
    let Some(statement @ Stmt::FunctionDef(function)) = parsed.suite().first() else { return Guard::default() };
    let p = &function.parameters;
    let parameters: BTreeSet<String> =
        p.posonlyargs.iter().chain(&p.args).chain(&p.kwonlyargs).map(|a| a.parameter.name.to_string()).collect();
    let mut body: &[Stmt] = &function.body;
    if let Some(Stmt::Expr(e)) = body.first() {
        if is_constant(&e.value) {
            body = &body[1..];
        }
    }
    let mut out = Guard::default();
    let mut empty_returns = BTreeSet::new();
    let mut leading = 0;
    for statement in body {
        let Some((name, kind, returned)) = leading_guard(statement, &parameters) else { break };
        leading += 1;
        if kind == Kind::Falsy {
            if is_empty_value(returned) {
                empty_returns.insert(name.clone());
            }
            out.falsy.insert(name);
        } else {
            out.booleans.insert(name);
        }
    }
    let mut later = Vec::new();
    for statement in &body[leading..] {
        returns(statement, &mut later);
    }
    let other_empty = later.into_iter().any(is_empty_value);
    let ends = match body.last() {
        Some(Stmt::Raise(_)) => true,
        Some(Stmt::Return(r)) => !is_empty_value(r.value.as_deref()),
        _ => false,
    };
    if empty_returns.len() == 1 && !other_empty && ends {
        out.none_only_for = empty_returns;
    }
    out.falsy.extend(guarded_parameters(function, &parameters, passes));
    if let Some(star) = &p.vararg {
        let mut check = CallableFirst { star: star.name.as_str(), found: false };
        check.visit_stmt(statement);
        out.callable_first = check.found;
    }
    out
}

fn guarded_parameters(function: &StmtFunctionDef, parameters: &BTreeSet<String>, passes: &mut Passes<'_>) -> Vec<String> {
    let mut found = Vec::new();
    for name in parameters {
        let mut reads = Reads { name, passes: &mut *passes, count: 0, other: false };
        reads.block(function.body.iter().map(S::Stmt).collect(), false);
        if reads.count > 0 && !reads.other {
            found.push(name.clone());
        }
    }
    found
}

/// A statement as Python's tree has it: `elif` is an `If` in the `orelse` of the one before.
#[derive(Clone, Copy)]
enum S<'a> {
    Stmt(&'a Stmt),
    Elif(&'a StmtIf, usize),
}

struct IfView<'a> {
    test: &'a Expr,
    body: &'a [Stmt],
    orelse: Vec<S<'a>>,
}

fn orelse_of(s: &StmtIf, i: usize) -> Vec<S<'_>> {
    match s.elif_else_clauses.get(i) {
        None => vec![],
        Some(clause) if clause.test.is_some() => vec![S::Elif(s, i)],
        Some(clause) => clause.body.iter().map(S::Stmt).collect(),
    }
}

fn if_view(s: S<'_>) -> Option<IfView<'_>> {
    match s {
        S::Stmt(Stmt::If(i)) => Some(IfView { test: &i.test, body: &i.body, orelse: orelse_of(i, 0) }),
        S::Elif(i, n) => {
            let clause = &i.elif_else_clauses[n];
            Some(IfView { test: clause.test.as_ref().unwrap(), body: &clause.body, orelse: orelse_of(i, n + 1) })
        }
        S::Stmt(_) => None,
    }
}

struct Reads<'r, 's> {
    name: &'r str,
    passes: &'r mut Passes<'s>,
    count: usize,
    other: bool,
}

struct Closed<'n> {
    name: &'n str,
    found: bool,
}

impl<'a> Visitor<'a> for Closed<'_> {
    fn visit_expr(&mut self, e: &'a Expr) {
        if named(e, self.name) {
            self.found = true;
        }
        walk_expr(self, e);
    }
    fn visit_parameter(&mut self, p: &'a Parameter) {
        if p.name.as_str() == self.name {
            self.found = true;
        }
        walk_parameter(self, p);
    }
}

impl Reads<'_, '_> {
    fn block(&mut self, statements: Vec<S<'_>>, mut proven: bool) {
        for statement in statements {
            proven = self.statement(statement, proven);
        }
    }
    fn stmts(&mut self, statements: &[Stmt], proven: bool) {
        self.block(statements.iter().map(S::Stmt).collect(), proven);
    }
    /// The statements of a `for`, `with`, `try` or `case` (`_parts`): each one only with
    /// what was proven before the statement that holds them.
    fn each(&mut self, statements: &[Stmt], proven: bool) {
        for statement in statements {
            self.statement(S::Stmt(statement), proven);
        }
    }

    fn statement(&mut self, node: S<'_>, proven: bool) -> bool {
        if let Some(view) = if_view(node) {
            self.expression(view.test, proven, true);
            self.stmts(view.body, proven || self.implies(view.test));
            let refutes = self.refutes(view.test);
            let empty = view.orelse.is_empty();
            self.block(view.orelse, proven || refutes);
            let leaves = matches!(view.body.last(), Some(Stmt::Return(_) | Stmt::Raise(_) | Stmt::Continue(_) | Stmt::Break(_)));
            return proven || (refutes && empty && leaves);
        }
        let S::Stmt(node) = node else { unreachable!() };
        match node {
            Stmt::While(w) => {
                self.expression(&w.test, proven, true);
                self.stmts(&w.body, proven || self.implies(&w.test));
                self.stmts(&w.orelse, proven);
                proven
            }
            Stmt::Assert(a) => {
                self.expression(&a.test, proven, true);
                if let Some(msg) = &a.msg {
                    self.expression(msg, proven, false);
                }
                proven || self.implies(&a.test)
            }
            Stmt::FunctionDef(_) | Stmt::ClassDef(_) => {
                let mut closed = Closed { name: self.name, found: false };
                closed.visit_stmt(node);
                self.close(closed.found);
                proven
            }
            other => {
                self.parts(other, proven);
                proven
            }
        }
    }

    fn close(&mut self, found: bool) {
        if found {
            self.count += 1;
            self.other = true;
        }
    }

    fn binds(&mut self, name: Option<&str>) {
        if name == Some(self.name) {
            self.other = true;
        }
    }

    fn exprs<'e>(&mut self, items: impl IntoIterator<Item = &'e Expr>, proven: bool) {
        for item in items {
            self.expression(item, proven, false);
        }
    }

    /// The statements and expressions of another statement (`_parts`).
    fn parts(&mut self, node: &Stmt, proven: bool) {
        match node {
            Stmt::Return(r) => self.exprs(r.value.as_deref(), proven),
            Stmt::Delete(d) => self.exprs(&d.targets, proven),
            Stmt::Assign(a) => {
                self.exprs(&a.targets, proven);
                self.expression(&a.value, proven, false);
            }
            Stmt::AugAssign(a) => {
                self.expression(&a.target, proven, false);
                self.expression(&a.value, proven, false);
            }
            Stmt::AnnAssign(a) => {
                self.expression(&a.target, proven, false);
                self.expression(&a.annotation, proven, false);
                self.exprs(a.value.as_deref(), proven);
            }
            Stmt::TypeAlias(t) => {
                self.expression(&t.name, proven, false);
                if let Some(params) = &t.type_params {
                    for param in &params.type_params {
                        match param {
                            TypeParam::TypeVar(v) => {
                                self.binds(Some(v.name.as_str()));
                                self.exprs(v.bound.as_deref(), proven);
                                self.exprs(v.default.as_deref(), proven);
                            }
                            TypeParam::ParamSpec(v) => {
                                self.binds(Some(v.name.as_str()));
                                self.exprs(v.default.as_deref(), proven);
                            }
                            TypeParam::TypeVarTuple(v) => {
                                self.binds(Some(v.name.as_str()));
                                self.exprs(v.default.as_deref(), proven);
                            }
                        }
                    }
                }
                self.expression(&t.value, proven, false);
            }
            Stmt::For(f) => {
                self.expression(&f.target, proven, false);
                self.expression(&f.iter, proven, false);
                self.each(&f.body, proven);
                self.each(&f.orelse, proven);
            }
            Stmt::With(w) => {
                for item in &w.items {
                    self.expression(&item.context_expr, proven, false);
                    self.exprs(item.optional_vars.as_deref(), proven);
                }
                self.each(&w.body, proven);
            }
            Stmt::Match(m) => {
                self.expression(&m.subject, proven, false);
                for case in &m.cases {
                    self.pattern(&case.pattern, proven);
                    self.exprs(case.guard.as_deref(), proven);
                    self.each(&case.body, proven);
                }
            }
            Stmt::Raise(r) => {
                self.exprs(r.exc.as_deref(), proven);
                self.exprs(r.cause.as_deref(), proven);
            }
            Stmt::Try(t) => {
                self.each(&t.body, proven);
                for handler in &t.handlers {
                    let ExceptHandler::ExceptHandler(h) = handler;
                    self.binds(h.name.as_ref().map(|n| n.as_str()));
                    self.exprs(h.type_.as_deref(), proven);
                    self.each(&h.body, proven);
                }
                self.each(&t.orelse, proven);
                self.each(&t.finalbody, proven);
            }
            Stmt::Import(i) => i.names.iter().for_each(|a| self.binds(Some(a.name.as_str()))),
            Stmt::ImportFrom(i) => i.names.iter().for_each(|a| self.binds(Some(a.name.as_str()))),
            Stmt::Expr(e) => self.expression(&e.value, proven, false),
            Stmt::Global(_)
            | Stmt::Nonlocal(_)
            | Stmt::Pass(_)
            | Stmt::Break(_)
            | Stmt::Continue(_)
            | Stmt::IpyEscapeCommand(_) => {}
            Stmt::If(_) | Stmt::While(_) | Stmt::Assert(_) | Stmt::FunctionDef(_) | Stmt::ClassDef(_) => {
                self.statement(S::Stmt(node), proven);
            }
        }
    }

    fn pattern(&mut self, p: &Pattern, proven: bool) {
        match p {
            Pattern::MatchValue(v) => self.expression(&v.value, proven, false),
            Pattern::MatchSingleton(_) => {}
            Pattern::MatchSequence(s) => s.patterns.iter().for_each(|q| self.pattern(q, proven)),
            Pattern::MatchMapping(m) => {
                self.binds(m.rest.as_ref().map(|n| n.as_str()));
                self.exprs(&m.keys, proven);
                m.patterns.iter().for_each(|q| self.pattern(q, proven));
            }
            Pattern::MatchClass(c) => {
                self.expression(&c.cls, proven, false);
                c.arguments.patterns.iter().for_each(|q| self.pattern(q, proven));
                c.arguments.keywords.iter().for_each(|k| self.pattern(&k.pattern, proven));
            }
            Pattern::MatchStar(s) => self.binds(s.name.as_ref().map(|n| n.as_str())),
            Pattern::MatchAs(a) => {
                self.binds(a.name.as_ref().map(|n| n.as_str()));
                if let Some(q) = &a.pattern {
                    self.pattern(q, proven);
                }
            }
            Pattern::MatchOr(o) => o.patterns.iter().for_each(|q| self.pattern(q, proven)),
        }
    }

    fn expression(&mut self, node: &Expr, proven: bool, test: bool) {
        match node {
            Expr::Name(n) => {
                if n.id.as_str() == self.name {
                    self.count += 1;
                    if n.ctx != ExprContext::Load || !(test || proven) {
                        self.other = true;
                    }
                }
            }
            Expr::BoolOp(b) => {
                let last = b.values.len() - 1;
                let mut proven = proven;
                for (index, value) in b.values.iter().enumerate() {
                    let kept = b.op == BoolOp::Or && index < last;
                    self.expression(value, proven, test || kept);
                    let reached = if b.op == BoolOp::And { self.implies(value) } else { self.refutes(value) };
                    proven = proven || reached;
                }
            }
            Expr::UnaryOp(u) if u.op == UnaryOp::Not => self.expression(&u.operand, proven, true),
            Expr::If(i) => {
                self.expression(&i.test, proven, true);
                self.expression(&i.body, proven || self.implies(&i.test), test);
                self.expression(&i.orelse, proven || self.refutes(&i.test), test);
            }
            Expr::Lambda(_) => {
                let mut closed = Closed { name: self.name, found: false };
                closed.visit_expr(node);
                self.close(closed.found);
            }
            Expr::Call(c) => self.call(c, proven),
            other => self.operands(other, proven),
        }
    }

    fn call(&mut self, node: &ExprCall, proven: bool) {
        self.expression(&node.func, proven, false);
        let mut positional = true;
        for (index, argument) in node.arguments.args.iter().enumerate() {
            positional = positional && !matches!(argument, Expr::Starred(_));
            if positional && named(argument, self.name) && self.passes.passes(node, Where::Index(index)) {
                self.count += 1;
            } else {
                self.expression(argument, proven, false);
            }
        }
        for keyword in &node.arguments.keywords {
            let passed = match &keyword.arg {
                Some(arg) => named(&keyword.value, self.name) && self.passes.passes(node, Where::Keyword(arg.as_str())),
                None => false,
            };
            if passed {
                self.count += 1;
            } else {
                self.expression(&keyword.value, proven, false);
            }
        }
    }

    fn interpolations(&mut self, elements: &[InterpolatedStringElement], proven: bool) {
        for element in elements {
            if let InterpolatedStringElement::Interpolation(i) = element {
                self.expression(&i.expression, proven, false);
                if let Some(spec) = &i.format_spec {
                    self.interpolations(&spec.elements, proven);
                }
            }
        }
    }

    fn comprehensions(&mut self, generators: &[ruff_python_ast::Comprehension], proven: bool) {
        for generator in generators {
            self.expression(&generator.target, proven, false);
            self.expression(&generator.iter, proven, false);
            for condition in &generator.ifs {
                self.expression(condition, proven, true);
            }
        }
    }

    fn operands(&mut self, node: &Expr, proven: bool) {
        match node {
            Expr::Named(n) => {
                self.expression(&n.target, proven, false);
                self.expression(&n.value, proven, false);
            }
            Expr::BinOp(b) => {
                self.expression(&b.left, proven, false);
                self.expression(&b.right, proven, false);
            }
            Expr::UnaryOp(u) => self.expression(&u.operand, proven, false),
            Expr::Dict(d) => {
                for item in &d.items {
                    self.exprs(item.key.as_ref(), proven);
                    self.expression(&item.value, proven, false);
                }
            }
            Expr::Set(s) => self.exprs(&s.elts, proven),
            Expr::List(l) => self.exprs(&l.elts, proven),
            Expr::Tuple(t) => self.exprs(&t.elts, proven),
            Expr::ListComp(c) => {
                self.expression(&c.elt, proven, false);
                self.comprehensions(&c.generators, proven);
            }
            Expr::SetComp(c) => {
                self.expression(&c.elt, proven, false);
                self.comprehensions(&c.generators, proven);
            }
            Expr::Generator(c) => {
                self.expression(&c.elt, proven, false);
                self.comprehensions(&c.generators, proven);
            }
            Expr::DictComp(c) => {
                self.expression(&c.key, proven, false);
                self.expression(&c.value, proven, false);
                self.comprehensions(&c.generators, proven);
            }
            Expr::Await(a) => self.expression(&a.value, proven, false),
            Expr::Yield(y) => self.exprs(y.value.as_deref(), proven),
            Expr::YieldFrom(y) => self.expression(&y.value, proven, false),
            Expr::Compare(c) => {
                self.expression(&c.left, proven, false);
                self.exprs(&c.comparators, proven);
            }
            Expr::FString(f) => {
                for part in &f.value {
                    if let ruff_python_ast::FStringPart::FString(s) = part {
                        self.interpolations(&s.elements, proven);
                    }
                }
            }
            Expr::TString(t) => {
                for s in &t.value {
                    self.interpolations(&s.elements, proven);
                }
            }
            Expr::Attribute(a) => self.expression(&a.value, proven, false),
            Expr::Subscript(s) => {
                self.expression(&s.value, proven, false);
                self.expression(&s.slice, proven, false);
            }
            Expr::Starred(s) => self.expression(&s.value, proven, false),
            Expr::Slice(s) => {
                self.exprs(s.lower.as_deref(), proven);
                self.exprs(s.upper.as_deref(), proven);
                self.exprs(s.step.as_deref(), proven);
            }
            Expr::Call(c) => {
                self.expression(&c.func, proven, false);
                self.exprs(&c.arguments.args, proven);
                c.arguments.keywords.iter().for_each(|k| self.expression(&k.value, proven, false));
            }
            _ => {} // names and literals have no operands; the rest were handled
        }
    }

    fn implies(&self, test: &Expr) -> bool {
        match test {
            Expr::Name(n) => n.id.as_str() == self.name,
            Expr::BoolOp(b) if b.op == BoolOp::And => b.values.iter().any(|v| self.implies(v)),
            Expr::UnaryOp(u) if u.op == UnaryOp::Not => self.refutes(&u.operand),
            _ => false,
        }
    }

    fn refutes(&self, test: &Expr) -> bool {
        match test {
            Expr::UnaryOp(u) if u.op == UnaryOp::Not => self.implies(&u.operand),
            Expr::BoolOp(b) if b.op == BoolOp::Or => b.values.iter().any(|v| self.refutes(v)),
            _ => false,
        }
    }
}
