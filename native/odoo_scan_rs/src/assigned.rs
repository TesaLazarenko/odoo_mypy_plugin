//! Port of `odoo_typing.scanner.assigned`: what the methods of a class assign to the
//! attributes of records (`assigned_values`, `filled_attributes`).
use ruff_python_ast::visitor::{walk_except_handler, walk_expr, Visitor};
use ruff_python_ast::{
    BoolOp, ExceptHandler, Expr, ExprCall, ExprContext, Operator, Stmt, StmtClassDef, StmtFor,
    StmtFunctionDef, StmtIf,
};
use std::collections::{BTreeSet, HashSet};

const TEXT: &str = "";
const TRUTHY: &str = "+";
const ANY_VALUE: &str = "?";
const ANY_NAME: &str = "*";
const OVERRIDDEN: &str = "^";
const MARKS: [&str; 3] = [TEXT, TRUTHY, ANY_VALUE];

type Kinds = BTreeSet<String>;
type State = Option<BTreeSet<String>>;

/// A statement as Python's tree has it (`elif` is an `If` in the `orelse` before it).
#[derive(Clone, Copy)]
enum S<'a> {
    Stmt(&'a Stmt),
    Elif(&'a StmtIf, usize),
}

fn items(body: &[Stmt]) -> Vec<S<'_>> {
    body.iter().map(S::Stmt).collect()
}

/// `(test, body, orelse)` of an `If` in Python's shape.
fn if_parts<'a>(s: S<'a>) -> Option<(&'a [Stmt], Vec<S<'a>>)> {
    let (i, body, next) = match s {
        S::Stmt(Stmt::If(i)) => (i, &i.body[..], 0),
        S::Elif(i, n) => (i, &i.elif_else_clauses[n].body[..], n + 1),
        _ => return None,
    };
    let orelse = match i.elif_else_clauses.get(next) {
        None => vec![],
        Some(c) if c.test.is_some() => vec![S::Elif(i, next)],
        Some(c) => items(&c.body),
    };
    Some((body, orelse))
}

/// The blocks Python's `getattr(statement, b)` for b in body/orelse/finalbody gives,
/// then the handlers' and the cases' bodies.
fn blocks<'a>(s: S<'a>) -> Vec<Vec<S<'a>>> {
    if let Some((body, orelse)) = if_parts(s) {
        return vec![items(body), orelse];
    }
    let S::Stmt(stmt) = s else { return vec![] };
    match stmt {
        Stmt::For(f) => vec![items(&f.body), items(&f.orelse)],
        Stmt::While(w) => vec![items(&w.body), items(&w.orelse)],
        Stmt::With(w) => vec![items(&w.body)],
        Stmt::FunctionDef(f) => vec![items(&f.body)],
        Stmt::ClassDef(c) => vec![items(&c.body)],
        Stmt::Try(t) => {
            let mut b = vec![items(&t.body), items(&t.orelse), items(&t.finalbody)];
            for ExceptHandler::ExceptHandler(h) in &t.handlers {
                b.push(items(&h.body));
            }
            b
        }
        Stmt::Match(m) => m.cases.iter().map(|c| items(&c.body)).collect(),
        _ => vec![],
    }
}

fn text(e: &Expr) -> Option<String> {
    match e {
        Expr::StringLiteral(s) => Some(s.value.to_str().to_string()),
        _ => None,
    }
}

fn start(mut node: &Expr) -> Option<&str> {
    loop {
        node = match node {
            Expr::Attribute(a) => &a.value,
            Expr::Subscript(s) => &s.value,
            Expr::Call(c) => &c.func,
            Expr::Name(n) => return Some(n.id.as_str()),
            _ => return None,
        };
    }
}

fn path(mut node: &Expr) -> Option<Vec<String>> {
    let mut parts = vec![];
    while let Expr::Attribute(a) = node {
        parts.push(a.attr.as_str().to_string());
        node = &a.value;
    }
    if matches!(node, Expr::Name(_)) {
        parts.reverse();
        Some(parts)
    } else {
        None
    }
}

fn dotted(mut node: &Expr) -> Option<String> {
    let mut parts = vec![];
    while let Expr::Attribute(a) = node {
        parts.push(a.attr.as_str().to_string());
        node = &a.value;
    }
    let Expr::Name(n) = node else { return None };
    parts.push(n.id.as_str().to_string());
    parts.reverse();
    Some(parts.join("."))
}

/// (name, value, receiver, of_records)
type Write<'a> = (String, Option<&'a Expr>, &'a Expr, bool);

fn targets<'a>(target: &'a Expr, value: Option<&'a Expr>, out: &mut Vec<Write<'a>>) {
    match target {
        Expr::Attribute(a) => out.push((a.attr.as_str().to_string(), value, &a.value, false)),
        Expr::Subscript(s) => {
            if let Some(name) = text(&s.slice) {
                out.push((name, value, &s.value, true));
            }
        }
        Expr::Tuple(_) | Expr::List(_) => {
            let elts = match target {
                Expr::Tuple(t) => &t.elts,
                Expr::List(l) => &l.elts,
                _ => unreachable!(),
            };
            let given: Vec<Option<&Expr>> = match value {
                Some(Expr::Tuple(v)) if v.elts.len() == elts.len() => v.elts.iter().map(Some).collect(),
                Some(Expr::List(v)) if v.elts.len() == elts.len() => v.elts.iter().map(Some).collect(),
                _ => vec![None; elts.len()],
            };
            for (item, g) in elts.iter().zip(given) {
                targets(item, g, out);
            }
        }
        _ => {}
    }
}

fn written<'a>(call: &'a ExprCall, out: &mut Vec<Write<'a>>) {
    let args = &call.arguments.args;
    match &*call.func {
        Expr::Attribute(f) if ["update", "write"].contains(&f.attr.as_str()) && !args.is_empty() => {
            if let Expr::Dict(d) = &args[0] {
                for item in &d.items {
                    match item.key.as_ref().and_then(text) {
                        Some(name) => out.push((name, Some(&item.value), &f.value, true)),
                        None => out.push((ANY_NAME.to_string(), None, &f.value, true)),
                    }
                }
            }
        }
        Expr::Name(n) if n.id.as_str() == "setattr" && args.len() == 3 => match text(&args[1]) {
            None => out.push((ANY_NAME.to_string(), None, &args[0], false)),
            Some(name) => out.push((name, Some(&args[2]), &args[0], false)),
        },
        _ => {}
    }
}

fn bind(target: &Expr, value: &Expr, bindings: &mut Vec<(String, String)>) {
    if let (Expr::Name(n), Some(s)) = (target, start(value)) {
        bindings.push((n.id.as_str().to_string(), s.to_string()));
    }
}

fn writes_of<'a>(statements: &'a [Stmt]) -> (Vec<Write<'a>>, Vec<(String, String)>) {
    let mut writes = vec![];
    let mut bindings = vec![];
    let mut pending: Vec<S<'a>> = items(statements);
    while let Some(s) = pending.pop() {
        match s {
            S::Stmt(Stmt::Assign(a)) => {
                for target in &a.targets {
                    targets(target, Some(&a.value), &mut writes);
                    bind(target, &a.value, &mut bindings);
                }
            }
            S::Stmt(Stmt::AnnAssign(a)) => {
                if let Some(value) = &a.value {
                    targets(&a.target, Some(value), &mut writes);
                    bind(&a.target, value, &mut bindings);
                }
            }
            S::Stmt(Stmt::AugAssign(a)) => targets(&a.target, None, &mut writes),
            S::Stmt(Stmt::Expr(e)) => {
                if let Expr::Call(c) = &*e.value {
                    written(c, &mut writes);
                }
            }
            other => {
                if let S::Stmt(Stmt::For(f)) = other {
                    bind(&f.target, &f.iter, &mut bindings);
                }
                for block in blocks(other) {
                    pending.extend(block);
                }
            }
        }
    }
    (writes, bindings)
}

fn kinds(value: &Expr) -> Kinds {
    let one = |k: &str| -> Kinds { [k.to_string()].into() };
    match value {
        Expr::StringLiteral(_) | Expr::FString(_) => one(TEXT),
        Expr::NumberLiteral(_) | Expr::BooleanLiteral(_) | Expr::NoneLiteral(_) | Expr::BytesLiteral(_) | Expr::EllipsisLiteral(_) => one(ANY_VALUE),
        Expr::If(i) => kinds(&i.body).union(&kinds(&i.orelse)).cloned().collect(),
        Expr::Named(n) => kinds(&n.value),
        Expr::BoolOp(b) if b.op == BoolOp::Or => {
            let mut first: Kinds = b.values[..b.values.len() - 1]
                .iter()
                .flat_map(kinds)
                .map(|k| if k == TEXT { TEXT.to_string() } else { TRUTHY.to_string() })
                .collect();
            first.extend(kinds(b.values.last().unwrap()));
            first
        }
        Expr::BinOp(b) if matches!(b.op, Operator::Mod | Operator::Add) => {
            let left = kinds(&b.left);
            if !left.contains(ANY_VALUE) || b.op == Operator::Mod {
                left
            } else {
                kinds(&b.right)
            }
        }
        Expr::Call(c) => {
            if let Expr::Attribute(f) = &*c.func {
                if ["format", "join"].contains(&f.attr.as_str()) {
                    let receiver = kinds(&f.value);
                    if !receiver.contains(ANY_VALUE) {
                        return receiver;
                    }
                }
            }
            match dotted(&c.func) {
                Some(d) => one(&d),
                None => one(ANY_VALUE),
            }
        }
        _ => one(ANY_VALUE),
    }
}

fn bound_names(function: &StmtFunctionDef) -> HashSet<String> {
    struct Bound<'f> {
        names: HashSet<String>,
        function: &'f StmtFunctionDef,
    }
    impl<'a> Visitor<'a> for Bound<'_> {
        fn visit_stmt(&mut self, stmt: &'a Stmt) {
            match stmt {
                Stmt::FunctionDef(f) if !std::ptr::eq(f, self.function) => {
                    self.names.insert(f.name.as_str().to_string());
                }
                Stmt::ClassDef(c) => {
                    self.names.insert(c.name.as_str().to_string());
                }
                _ => {}
            }
            crate::guards::walk_stmt_once(self, stmt);
        }
        fn visit_expr(&mut self, expr: &'a Expr) {
            if let Expr::Name(n) = expr {
                if n.ctx == ExprContext::Store {
                    self.names.insert(n.id.as_str().to_string());
                }
            }
            walk_expr(self, expr);
        }
        fn visit_parameter(&mut self, p: &'a ruff_python_ast::Parameter) {
            self.names.insert(p.name.as_str().to_string());
            ruff_python_ast::visitor::walk_parameter(self, p);
        }
        fn visit_alias(&mut self, alias: &'a ruff_python_ast::Alias) {
            let name = alias.asname.as_ref().unwrap_or(&alias.name).as_str();
            self.names.insert(name.split('.').next().unwrap().to_string());
        }
        fn visit_except_handler(&mut self, handler: &'a ExceptHandler) {
            let ExceptHandler::ExceptHandler(h) = handler;
            if let Some(n) = &h.name {
                if !n.as_str().is_empty() {
                    self.names.insert(n.as_str().to_string());
                }
            }
            walk_except_handler(self, handler);
        }
    }
    let mut b = Bound { names: HashSet::new(), function };
    for d in &function.decorator_list {
        b.visit_decorator(d);
    }
    b.visit_parameters(&function.parameters);
    if let Some(r) = &function.returns {
        b.visit_annotation(r);
    }
    b.visit_body(&function.body);
    b.names
}

pub struct Assignments {
    pub known: Vec<(String, String, Vec<String>)>,
    pub unknown: Vec<String>,
    pub through: Vec<(String, Vec<String>)>,
    pub filled: Vec<(String, Vec<String>)>,
}

pub fn assigned_values(node: &StmtClassDef) -> Assignments {
    let mut known = vec![];
    let mut unknown: BTreeSet<String> = BTreeSet::new();
    let mut through: BTreeSet<(String, Vec<String>)> = BTreeSet::new();
    let mut filled = vec![];
    for statement in &node.body {
        let Stmt::FunctionDef(f) = statement else { continue };
        let (writes, bindings) = writes_of(&f.body);
        let records = records_of(f, &bindings);
        let mut order: Vec<String> = vec![];
        let mut values: std::collections::HashMap<String, Kinds> = Default::default();
        for (name, value, receiver, of_records) in writes {
            let s = start(receiver);
            let in_records = s.is_some_and(|s| records.contains(s));
            if of_records && !in_records {
                continue;
            }
            let k = match value {
                Some(v) => kinds(v),
                None => [ANY_VALUE.to_string()].into(),
            };
            let p = if in_records { path(receiver) } else { None };
            if let Some(p) = &p {
                if !p.is_empty() && name != ANY_NAME && k.contains(ANY_VALUE) {
                    through.insert((name, p.clone()));
                    continue;
                }
            }
            if !values.contains_key(&name) {
                order.push(name.clone());
            }
            values.entry(name).or_default().extend(k);
        }
        // _drop_local_calls
        if values.values().any(|k| k.iter().any(|x| !MARKS.contains(&x.as_str()))) {
            let bound = bound_names(f);
            for k in values.values_mut() {
                let calls: Vec<&String> = k.iter().filter(|x| !MARKS.contains(&x.as_str())).collect();
                if !calls.is_empty() && calls.iter().any(|c| bound.contains(c.split('.').next().unwrap())) {
                    k.insert(ANY_VALUE.to_string());
                }
            }
        }
        let mut given: BTreeSet<String> = BTreeSet::new();
        for name in order {
            let k = &values[&name];
            if name == ANY_NAME {
                known.push((f.name.as_str().to_string(), name, vec![ANY_VALUE.to_string()]));
            } else if k.contains(ANY_VALUE) {
                unknown.insert(name);
            } else {
                known.push((f.name.as_str().to_string(), name.clone(), k.iter().cloned().collect()));
                given.insert(name);
            }
        }
        if !given.is_empty() || hands_on(f) {
            let mut allowed = given.clone();
            allowed.insert(OVERRIDDEN.to_string());
            let fills: Vec<String> = filled_attributes(f).intersection(&allowed).cloned().collect();
            if !fills.is_empty() {
                filled.push((f.name.as_str().to_string(), fills));
            }
        }
    }
    Assignments { known, unknown: unknown.into_iter().collect(), through: through.into_iter().collect(), filled }
}

fn records_of(f: &StmtFunctionDef, bindings: &[(String, String)]) -> HashSet<String> {
    let mut records: HashSet<String> = f
        .parameters
        .posonlyargs
        .iter()
        .chain(&f.parameters.args)
        .next()
        .map(|a| a.parameter.name.as_str().to_string())
        .into_iter()
        .collect();
    let mut changed = !records.is_empty();
    while changed {
        changed = false;
        for (name, s) in bindings {
            if records.contains(s) && !records.contains(name) {
                records.insert(name.clone());
                changed = true;
            }
        }
    }
    records
}

fn first_parameter(f: &StmtFunctionDef) -> Option<&str> {
    f.parameters.posonlyargs.iter().chain(&f.parameters.args).next().map(|a| a.parameter.name.as_str())
}

/// Shared lists of states (Python passes the same list objects around).
struct Lists(Vec<Vec<BTreeSet<String>>>);

#[derive(Clone)]
struct Paths {
    receiver: String,
    method: String,
    exits: usize,
    returns: Option<usize>,
    before: BTreeSet<String>,
    inner: bool,
}

fn meet(states: &[State]) -> State {
    let reached: Vec<&BTreeSet<String>> = states.iter().flatten().collect();
    let first = reached.first()?;
    Some(reached[1..].iter().fold((*first).clone(), |acc, s| acc.intersection(s).cloned().collect()))
}

fn common(states: &[State]) -> BTreeSet<String> {
    meet(states).unwrap_or_default()
}

fn is_name(e: &Expr, name: &str) -> bool {
    matches!(e, Expr::Name(n) if n.id.as_str() == name)
}

fn overridden(node: &Expr, paths: &Paths) -> bool {
    if paths.returns.is_some() {
        return false;
    }
    let Expr::Call(c) = node else { return false };
    let Expr::Attribute(f) = &*c.func else { return false };
    if f.attr.as_str() != paths.method {
        return false;
    }
    let Expr::Call(owner) = &*f.value else { return false };
    matches!(&*owner.func, Expr::Name(n) if n.id.as_str() == "super")
        && (owner.arguments.args.is_empty() || is_name(owner.arguments.args.last().unwrap(), &paths.receiver))
}

fn assigned_to(targets: &[&Expr], receiver: &str) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    let mut pending: Vec<&Expr> = targets.to_vec();
    while let Some(t) = pending.pop() {
        match t {
            Expr::Tuple(x) => pending.extend(x.elts.iter()),
            Expr::List(x) => pending.extend(x.elts.iter()),
            Expr::Starred(s) => pending.push(&s.value),
            Expr::Attribute(a) if is_name(&a.value, receiver) => {
                names.insert(a.attr.as_str().to_string());
            }
            Expr::Subscript(s) if is_name(&s.value, receiver) => {
                if let Some(n) = text(&s.slice) {
                    names.insert(n);
                }
            }
            _ => {}
        }
    }
    names
}

fn called(call: &ExprCall, node: &Expr, paths: &Paths) -> BTreeSet<String> {
    if overridden(node, paths) {
        return [OVERRIDDEN.to_string()].into();
    }
    let mut out = vec![];
    written(call, &mut out);
    out.into_iter()
        .filter(|(n, _, r, _)| n != ANY_NAME && is_name(r, &paths.receiver))
        .map(|(n, ..)| n)
        .collect()
}

fn stored_names(statements: &[Stmt]) -> HashSet<String> {
    let mut names = HashSet::new();
    let mut targets: Vec<&Expr> = vec![];
    let mut pending: Vec<S<'_>> = items(statements);
    while let Some(s) = pending.pop() {
        if let S::Stmt(stmt) = s {
            match stmt {
                Stmt::Assign(a) => targets.extend(a.targets.iter()),
                Stmt::AnnAssign(a) => targets.push(&a.target),
                Stmt::AugAssign(a) => targets.push(&a.target),
                Stmt::For(f) => targets.push(&f.target),
                Stmt::With(w) => targets.extend(w.items.iter().filter_map(|i| i.optional_vars.as_deref())),
                _ => {}
            }
            if matches!(stmt, Stmt::FunctionDef(_) | Stmt::ClassDef(_)) {
                continue;
            }
        }
        for block in blocks(s) {
            pending.extend(block);
        }
    }
    while let Some(t) = targets.pop() {
        match t {
            Expr::Name(n) => {
                names.insert(n.id.as_str().to_string());
            }
            Expr::Tuple(x) => targets.extend(x.elts.iter()),
            Expr::List(x) => targets.extend(x.elts.iter()),
            Expr::Starred(st) => targets.push(&st.value),
            _ => {}
        }
    }
    names
}

fn block(statements: &[S<'_>], mut state: State, paths: &Paths, lists: &mut Lists) -> State {
    for &s in statements {
        let Some(current) = state else { return None };
        state = statement(s, current, paths, lists);
    }
    state
}

fn statement(s: S<'_>, state: BTreeSet<String>, paths: &Paths, lists: &mut Lists) -> State {
    if let Some((body, orelse)) = if_parts(s) {
        let a = block(&items(body), Some(state.clone()), paths, lists);
        let b = block(&orelse, Some(state), paths, lists);
        return meet(&[a, b]);
    }
    let S::Stmt(stmt) = s else { unreachable!() };
    match stmt {
        Stmt::Assign(a) => {
            let t: Vec<&Expr> = a.targets.iter().collect();
            Some(&state | &assigned_to(&t, &paths.receiver))
        }
        Stmt::AnnAssign(a) if a.value.is_some() => Some(&state | &assigned_to(&[&a.target], &paths.receiver)),
        Stmt::Expr(e) if matches!(&*e.value, Expr::Call(_)) => {
            let Expr::Call(c) = &*e.value else { unreachable!() };
            Some(&state | &called(c, &e.value, paths))
        }
        Stmt::Return(r) => {
            if let Some(returns) = paths.returns {
                lists.0[paths.exits].push(BTreeSet::new());
                lists.0[returns].push(paths.before.clone());
                return None;
            }
            let mut st = state;
            if r.value.as_deref().is_some_and(|v| overridden(v, paths)) {
                st.insert(OVERRIDDEN.to_string());
            }
            lists.0[paths.exits].push(st);
            None
        }
        Stmt::Continue(_) | Stmt::Break(_) => {
            if paths.returns.is_some() && !paths.inner {
                let st = if matches!(stmt, Stmt::Continue(_)) { state } else { BTreeSet::new() };
                lists.0[paths.exits].push(st);
            }
            None
        }
        Stmt::Raise(_) => None,
        Stmt::For(f) if over_records(f, paths) => records_loop(f, state, paths, lists),
        Stmt::For(f) => {
            let inner = Paths { inner: true, ..paths.clone() };
            block(&items(&f.body), Some(state.clone()), &inner, lists);
            block(&items(&f.orelse), Some(state.clone()), &inner, lists);
            Some(state)
        }
        Stmt::While(w) => {
            let inner = Paths { inner: true, ..paths.clone() };
            block(&items(&w.body), Some(state.clone()), &inner, lists);
            block(&items(&w.orelse), Some(state.clone()), &inner, lists);
            Some(state)
        }
        Stmt::Try(t) => {
            let body = block(&items(&t.body), Some(state.clone()), paths, lists);
            let done = block(&items(&t.orelse), body, paths, lists);
            let mut all = vec![done];
            for ExceptHandler::ExceptHandler(h) in &t.handlers {
                all.push(block(&items(&h.body), Some(state.clone()), paths, lists));
            }
            block(&items(&t.finalbody), meet(&all), paths, lists)
        }
        Stmt::With(w) => {
            block(&items(&w.body), Some(state.clone()), paths, lists);
            Some(state)
        }
        Stmt::Match(m) => {
            for case in &m.cases {
                block(&items(&case.body), Some(state.clone()), paths, lists);
            }
            Some(state)
        }
        _ => Some(state),
    }
}

fn over_records(f: &StmtFor, paths: &Paths) -> bool {
    paths.returns.is_none() && !paths.inner && is_name(&f.iter, &paths.receiver)
}

fn records_loop(f: &StmtFor, state: BTreeSet<String>, paths: &Paths, lists: &mut Lists) -> State {
    let Expr::Name(target) = &*f.target else { return Some(state) };
    if stored_names(&f.body).contains(target.id.as_str()) {
        return Some(state);
    }
    lists.0.push(vec![]);
    let loop_exits = lists.0.len() - 1;
    let lp = Paths {
        receiver: target.id.as_str().to_string(),
        method: paths.method.clone(),
        exits: loop_exits,
        returns: Some(paths.exits),
        before: state.clone(),
        inner: false,
    };
    let end = block(&items(&f.body), Some(BTreeSet::new()), &lp, lists);
    let mut states: Vec<State> = lists.0[loop_exits].iter().cloned().map(Some).collect();
    states.push(end);
    let mut gained = common(&states);
    gained.remove(OVERRIDDEN);
    let after: BTreeSet<String> = &state | &gained;
    block(&items(&f.orelse), Some(after.clone()), paths, lists);
    Some(after)
}

pub fn filled_attributes(f: &StmtFunctionDef) -> BTreeSet<String> {
    let Some(records) = first_parameter(f) else { return BTreeSet::new() };
    if stored_names(&f.body).contains(records) {
        return BTreeSet::new();
    }
    let mut lists = Lists(vec![vec![]]);
    let paths = Paths { receiver: records.to_string(), method: f.name.as_str().to_string(), exits: 0, returns: None, before: BTreeSet::new(), inner: false };
    let end = block(&items(&f.body), Some(BTreeSet::new()), &paths, &mut lists);
    let mut states: Vec<State> = lists.0[0].iter().cloned().map(Some).collect();
    states.push(end);
    common(&states)
}

fn hands_on(f: &StmtFunctionDef) -> bool {
    let paths = Paths { receiver: first_parameter(f).unwrap_or("").to_string(), method: f.name.as_str().to_string(), exits: 0, returns: None, before: BTreeSet::new(), inner: false };
    f.body.iter().any(|s| match s {
        Stmt::Expr(e) => overridden(&e.value, &paths),
        Stmt::Return(r) => r.value.as_deref().is_some_and(|v| overridden(v, &paths)),
        _ => false,
    })
}
