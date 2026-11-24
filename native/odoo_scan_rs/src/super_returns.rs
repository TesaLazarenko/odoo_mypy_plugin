//! Port of `odoo_typing.scanner.super_returns`: the methods of a class that return
//! their `super()` call and nothing else.
use crate::guards::walk_stmt_once;
use ruff_python_ast::visitor::{walk_except_handler, walk_expr, walk_pattern, Visitor};
use ruff_python_ast::{ExceptHandler, Expr, ExprContext, Pattern, Stmt, StmtClassDef, StmtFunctionDef};
use std::collections::{HashMap, HashSet};

fn names_in(expr: &Expr, out: &mut Vec<String>) {
    struct Names<'o>(&'o mut Vec<String>);
    impl<'a> Visitor<'a> for Names<'_> {
        fn visit_expr(&mut self, expr: &'a Expr) {
            if let Expr::Name(n) = expr {
                self.0.push(n.id.as_str().to_string());
            }
            walk_expr(self, expr);
        }
    }
    Names(out).visit_expr(expr);
}

fn class_bindings(statement: &Stmt) -> Vec<String> {
    let mut out = vec![];
    match statement {
        Stmt::FunctionDef(f) => out.push(f.name.as_str().to_string()),
        Stmt::ClassDef(c) => out.push(c.name.as_str().to_string()),
        Stmt::Assign(a) => a.targets.iter().for_each(|t| names_in(t, &mut out)),
        Stmt::AnnAssign(a) => names_in(&a.target, &mut out),
        Stmt::AugAssign(a) => names_in(&a.target, &mut out),
        _ => {}
    }
    out
}

pub fn super_returns(node: &StmtClassDef) -> Vec<String> {
    let mut bound: HashMap<String, usize> = HashMap::new();
    for statement in &node.body {
        for name in class_bindings(statement) {
            *bound.entry(name).or_default() += 1;
        }
    }
    node.body
        .iter()
        .filter_map(|s| match s {
            Stmt::FunctionDef(f) if !f.is_async => Some(f),
            _ => None,
        })
        .filter(|f| bound.get(f.name.as_str()) == Some(&1) && passes_super(f, node.name.as_str()))
        .map(|f| f.name.as_str().to_string())
        .collect()
}

fn passes_super(function: &StmtFunctionDef, class_name: &str) -> bool {
    let p = &function.parameters;
    let positional: Vec<_> = p.posonlyargs.iter().chain(&p.args).collect();
    let mut every: Vec<&ruff_python_ast::Parameter> =
        positional.iter().map(|a| &a.parameter).chain(p.kwonlyargs.iter().map(|a| &a.parameter)).collect();
    every.extend(p.vararg.as_deref());
    every.extend(p.kwarg.as_deref());
    if function.returns.is_some() || every.iter().any(|a| a.annotation.is_some()) {
        return false;
    }
    if positional.is_empty() || !always_exits(&function.body) {
        return false;
    }
    let first = positional[0].parameter.name.as_str();
    let Some(returned) = returned_names(&function.body, function.name.as_str(), class_name, first) else {
        return false;
    };
    if every.iter().any(|a| returned.contains(a.name.as_str())) {
        return false;
    }
    bound_once(&function.body, &returned)
}

/// The statements of a block and of the blocks in it, not of nested functions or classes.
fn statements<'a>(body: &'a [Stmt], out: &mut Vec<&'a Stmt>) {
    for statement in body {
        out.push(statement);
        match statement {
            Stmt::FunctionDef(_) | Stmt::ClassDef(_) => {}
            Stmt::If(i) => {
                statements(&i.body, out);
                for clause in &i.elif_else_clauses {
                    statements(&clause.body, out);
                }
            }
            Stmt::For(f) => {
                statements(&f.body, out);
                statements(&f.orelse, out);
            }
            Stmt::While(w) => {
                statements(&w.body, out);
                statements(&w.orelse, out);
            }
            Stmt::With(w) => statements(&w.body, out),
            Stmt::Try(t) => {
                statements(&t.body, out);
                statements(&t.orelse, out);
                statements(&t.finalbody, out);
                for ExceptHandler::ExceptHandler(h) in &t.handlers {
                    statements(&h.body, out);
                }
            }
            Stmt::Match(m) => m.cases.iter().for_each(|c| statements(&c.body, out)),
            _ => {}
        }
    }
}

fn super_call(node: Option<&Expr>, method: &str, class_name: &str, first: &str) -> bool {
    let Some(Expr::Call(call)) = node else { return false };
    let Expr::Attribute(attribute) = &*call.func else { return false };
    let Expr::Call(made) = &*attribute.value else { return false };
    if attribute.attr.as_str() != method
        || !matches!(&*made.func, Expr::Name(n) if n.id.as_str() == "super")
        || !made.arguments.keywords.is_empty()
    {
        return false;
    }
    let args = &made.arguments.args;
    if args.is_empty() {
        return true;
    }
    args.len() == 2
        && matches!(&args[0], Expr::Name(n) if n.id.as_str() == class_name)
        && matches!(&args[1], Expr::Name(n) if n.id.as_str() == first)
}

fn returned_names(body: &[Stmt], method: &str, class_name: &str, first: &str) -> Option<HashSet<String>> {
    let mut all = vec![];
    statements(body, &mut all);
    let mut returned = HashSet::new();
    let mut found = false;
    for statement in &all {
        let Stmt::Return(r) = statement else { continue };
        found = true;
        match r.value.as_deref() {
            Some(Expr::Name(n)) => {
                returned.insert(n.id.as_str().to_string());
            }
            value => {
                if !super_call(value, method, class_name, first) {
                    return None;
                }
            }
        }
    }
    if !found {
        return None;
    }
    let mut bound = HashSet::new();
    for statement in &all {
        if let Stmt::Assign(a) = statement {
            if super_call(Some(&a.value), method, class_name, first) {
                for target in &a.targets {
                    if let Expr::Name(n) = target {
                        bound.insert(n.id.as_str().to_string());
                    }
                }
            }
        }
    }
    returned.is_subset(&bound).then_some(returned)
}

/// Counts the bindings of names in a method's own nodes (not of nested scopes).
struct Own<'n> {
    names: &'n HashSet<String>,
    counts: HashMap<String, usize>,
    fails: bool,
}

impl Own<'_> {
    fn bind(&mut self, name: &str) {
        if self.names.contains(name) {
            *self.counts.entry(name.to_string()).or_default() += 1;
        }
    }
}

impl<'a> Visitor<'a> for Own<'_> {
    fn visit_stmt(&mut self, stmt: &'a Stmt) {
        match stmt {
            Stmt::FunctionDef(f) => self.bind(f.name.as_str()),
            Stmt::ClassDef(c) => self.bind(c.name.as_str()),
            Stmt::Assign(a) => {
                let direct: Vec<&str> = a
                    .targets
                    .iter()
                    .filter_map(|t| if let Expr::Name(n) = t { Some(n.id.as_str()) } else { None })
                    .collect();
                for name in self.names.clone() {
                    if direct.contains(&name.as_str()) {
                        self.bind(&name);
                    }
                }
                for target in &a.targets {
                    if !matches!(target, Expr::Name(_)) {
                        self.visit_expr(target);
                    }
                }
                self.visit_expr(&a.value);
            }
            Stmt::AugAssign(a) => {
                if !matches!(&*a.target, Expr::Name(_)) {
                    self.visit_expr(&a.target);
                }
                self.visit_expr(&a.value);
            }
            Stmt::Global(g) => {
                if g.names.iter().any(|n| self.names.contains(n.as_str())) {
                    self.fails = true;
                }
            }
            Stmt::Nonlocal(g) => {
                if g.names.iter().any(|n| self.names.contains(n.as_str())) {
                    self.fails = true;
                }
            }
            _ => walk_stmt_once(self, stmt),
        }
    }
    fn visit_expr(&mut self, expr: &'a Expr) {
        match expr {
            Expr::Lambda(_) => {}
            Expr::Yield(_) | Expr::YieldFrom(_) => {
                self.fails = true;
                walk_expr(self, expr);
            }
            Expr::Name(n) => {
                if matches!(n.ctx, ExprContext::Store | ExprContext::Del) {
                    self.bind(n.id.as_str());
                }
            }
            _ => walk_expr(self, expr),
        }
    }
    fn visit_except_handler(&mut self, handler: &'a ExceptHandler) {
        let ExceptHandler::ExceptHandler(h) = handler;
        if let Some(name) = &h.name {
            self.bind(name.as_str());
        }
        walk_except_handler(self, handler);
    }
    fn visit_alias(&mut self, alias: &'a ruff_python_ast::Alias) {
        let bound = match &alias.asname {
            Some(asname) => asname.as_str(),
            None => alias.name.as_str().split('.').next().unwrap(),
        };
        self.bind(bound);
    }
    fn visit_pattern(&mut self, pattern: &'a Pattern) {
        match pattern {
            Pattern::MatchAs(p) => {
                if let Some(n) = &p.name {
                    self.bind(n.as_str());
                }
            }
            Pattern::MatchStar(p) => {
                if let Some(n) = &p.name {
                    self.bind(n.as_str());
                }
            }
            Pattern::MatchMapping(p) => {
                if let Some(n) = &p.rest {
                    self.bind(n.as_str());
                }
            }
            _ => {}
        }
        walk_pattern(self, pattern);
    }
}

fn bound_once(body: &[Stmt], names: &HashSet<String>) -> bool {
    let mut own = Own { names, counts: HashMap::new(), fails: false };
    own.visit_body(body);
    !own.fails && names.iter().all(|n| own.counts.get(n) == Some(&1))
}

fn always_exits(body: &[Stmt]) -> bool {
    let Some(last) = body.last() else { return false };
    match last {
        Stmt::Return(_) | Stmt::Raise(_) => true,
        Stmt::If(i) => always_exits(&i.body) && chain_exits(i, 0),
        Stmt::Try(t) => {
            if always_exits(&t.finalbody) {
                return true;
            }
            let ending = if t.orelse.is_empty() { &t.body } else { &t.orelse };
            always_exits(ending) && t.handlers.iter().all(|ExceptHandler::ExceptHandler(h)| always_exits(&h.body))
        }
        _ => false,
    }
}

/// `_always_exits` of the `orelse` an `If` has in Python's tree from clause `i` on.
fn chain_exits(i: &ruff_python_ast::StmtIf, n: usize) -> bool {
    match i.elif_else_clauses.get(n) {
        None => false,
        Some(clause) if clause.test.is_some() => always_exits(&clause.body) && chain_exits(i, n + 1),
        Some(clause) => always_exits(&clause.body),
    }
}
