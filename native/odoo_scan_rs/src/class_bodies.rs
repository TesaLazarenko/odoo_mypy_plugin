//! Port of the class-body parts of `odoo_typing.scanner.class_bodies` that read only
//! the class node: defined_methods, declared_comodels, read_value_keys,
//! record_dispatches, passed_containers. `ast.walk` order is approximated by a walk
//! in source order (the results are compared as sequences and as sets).
use crate::guards::walk_stmt_once;
use ruff_python_ast::visitor::{walk_expr, Visitor};
use ruff_python_ast::{CmpOp, Expr, ExprCall, ExprContext, Operator, Stmt, StmtClassDef, StmtFunctionDef};
use std::collections::{BTreeSet, HashMap, HashSet};

const RELATIONAL_FIELDS: [&str; 3] = ["Many2one", "One2many", "Many2many"];
const SEQUENCES: [&str; 3] = ["list", "set", "frozenset"];
const GIVEN: [&str; 3] = ["list", "set", "tuple"];
const MAX_PATH: usize = 4;

fn text(e: &Expr) -> Option<String> {
    match e {
        Expr::StringLiteral(s) => Some(s.value.to_str().to_string()),
        _ => None,
    }
}
fn callee_name(e: &Expr) -> Option<&str> {
    match e {
        Expr::Attribute(a) => Some(a.attr.as_str()),
        Expr::Name(n) => Some(n.id.as_str()),
        _ => None,
    }
}
fn comodel_argument(call: &ExprCall) -> Option<String> {
    match call.arguments.args.first() {
        Some(first) => text(first),
        None => call
            .arguments
            .keywords
            .iter()
            .find(|k| k.arg.as_ref().is_some_and(|a| a.as_str() == "comodel_name"))
            .and_then(|k| text(&k.value)),
    }
}

/// An ordered set (`dict.fromkeys` in Python).
#[derive(Default)]
struct Ordered<T: std::hash::Hash + Eq + Clone> {
    items: Vec<T>,
    seen: HashSet<T>,
}
impl<T: std::hash::Hash + Eq + Clone> Ordered<T> {
    fn add(&mut self, item: T) {
        if self.seen.insert(item.clone()) {
            self.items.push(item);
        }
    }
    fn contains(&self, item: &T) -> bool {
        self.seen.contains(item)
    }
}

fn mangled(name: &str, class_name: &str) -> String {
    if name.starts_with("__") && !name.ends_with("__") && !class_name.trim_matches('_').is_empty() {
        format!("_{}{}", class_name.trim_start_matches('_'), name)
    } else {
        name.to_string()
    }
}

pub fn defined_methods(node: &StmtClassDef) -> Vec<String> {
    let class_name = node.name.as_str();
    let mut names: Ordered<String> = Ordered::default();
    for statement in &node.body {
        match statement {
            Stmt::FunctionDef(f) => names.add(mangled(f.name.as_str(), class_name)),
            Stmt::Assign(a) => {
                let aliased = matches!(&*a.value, Expr::Name(n) if names.contains(&mangled(n.id.as_str(), class_name)));
                let function = matches!(&*a.value, Expr::Attribute(at) if at.attr.as_str().chars().next().is_some_and(|c| c.is_lowercase()));
                if aliased || function {
                    for target in &a.targets {
                        if let Expr::Name(n) = target {
                            names.add(mangled(n.id.as_str(), class_name));
                        }
                    }
                }
            }
            _ => {}
        }
    }
    names.items
}

fn assigned_value(statement: &Stmt) -> Option<&Expr> {
    match statement {
        Stmt::Assign(a) => Some(&a.value),
        Stmt::AnnAssign(a) => a.value.as_deref(),
        _ => None,
    }
}

pub fn declared_comodels(node: &StmtClassDef) -> Vec<String> {
    let mut comodels: Ordered<String> = Ordered::default();
    for statement in &node.body {
        if let Some(Expr::Call(call)) = assigned_value(statement) {
            if callee_name(&call.func).is_some_and(|n| RELATIONAL_FIELDS.contains(&n)) {
                if let Some(c) = comodel_argument(call).filter(|c| !c.is_empty()) {
                    comodels.add(c);
                }
            }
        }
    }
    comodels.items
}

/// The nodes of a function, as `ast.walk` gives them (here in source order).
enum Node<'a> {
    Stmt(&'a Stmt),
    Expr(&'a Expr),
    Comprehension(&'a ruff_python_ast::Comprehension),
}

fn names_of(e: &Expr, out: &mut HashSet<String>) {
    match e {
        Expr::Name(n) => {
            out.insert(n.id.as_str().to_string());
        }
        Expr::Tuple(t) => t.elts.iter().for_each(|x| names_of(x, out)),
        Expr::List(t) => t.elts.iter().for_each(|x| names_of(x, out)),
        _ => {}
    }
}

fn read_key(node: &Node<'_>, names: &HashSet<String>) -> Option<String> {
    let Node::Expr(e) = node else { return None };
    match e {
        Expr::Call(call) => {
            let Expr::Attribute(f) = &*call.func else { return None };
            let Expr::Name(v) = &*f.value else { return None };
            if ["pop", "get", "setdefault"].contains(&f.attr.as_str()) && names.contains(v.id.as_str()) {
                return call.arguments.args.first().and_then(text);
            }
            None
        }
        Expr::Subscript(s) => {
            let Expr::Name(v) = &*s.value else { return None };
            if matches!(s.ctx, ExprContext::Load | ExprContext::Del) && names.contains(v.id.as_str()) {
                return text(&s.slice);
            }
            None
        }
        Expr::Compare(c) => {
            if c.ops.len() == 1 && matches!(c.ops[0], CmpOp::In | CmpOp::NotIn) {
                if let Expr::Name(v) = &c.comparators[0] {
                    if names.contains(v.id.as_str()) {
                        return text(&c.left);
                    }
                }
            }
            None
        }
        _ => None,
    }
}

/// The function statements of a class body (`Stmt` and its function).
fn methods_of(node: &StmtClassDef) -> Vec<(&Stmt, &StmtFunctionDef)> {
    node.body
        .iter()
        .filter_map(|s| if let Stmt::FunctionDef(f) = s { Some((s, f)) } else { None })
        .collect()
}

fn nodes_of(stmt: &Stmt) -> Vec<Node<'_>> {
    struct All<'a>(Vec<Node<'a>>);
    impl<'a> Visitor<'a> for All<'a> {
        fn visit_stmt(&mut self, stmt: &'a Stmt) {
            self.0.push(Node::Stmt(stmt));
            walk_stmt_once(self, stmt);
        }
        fn visit_expr(&mut self, expr: &'a Expr) {
            self.0.push(Node::Expr(expr));
            walk_expr(self, expr);
        }
        fn visit_comprehension(&mut self, c: &'a ruff_python_ast::Comprehension) {
            self.0.push(Node::Comprehension(c));
            ruff_python_ast::visitor::walk_comprehension(self, c);
        }
    }
    let mut all = All(vec![]);
    all.visit_stmt(stmt);
    all.0
}

pub fn read_value_keys(node: &StmtClassDef) -> Vec<String> {
    let mut keys: Ordered<String> = Ordered::default();
    for (stmt, f) in methods_of(node) {
        if f.is_async || !["create", "write"].contains(&f.name.as_str()) {
            continue;
        }
        let params: Vec<_> = f.parameters.posonlyargs.iter().chain(&f.parameters.args).collect();
        if params.len() < 2 {
            continue;
        }
        let mut names: HashSet<String> = [params[1].parameter.name.as_str().to_string()].into();
        let nodes = nodes_of(stmt);
        for item in &nodes {
            let (iter, target) = match item {
                Node::Stmt(Stmt::For(f)) => (&*f.iter, &*f.target),
                Node::Comprehension(c) => (&c.iter, &c.target),
                _ => continue,
            };
            let mut iter_names = HashSet::new();
            names_of(iter, &mut iter_names);
            if !iter_names.is_disjoint(&names) {
                names_of(target, &mut names);
            }
        }
        let mut found: Vec<(usize, String)> = vec![];
        for item in &nodes {
            if let (Some(key), Node::Expr(e)) = (read_key(item, &names), item) {
                found.push((ruff_text_size::Ranged::start(*e).to_usize(), key));
            }
        }
        found.sort();
        for (_, key) in found {
            keys.add(key);
        }
    }
    keys.items
}

fn methods_map<'a>(node: &'a StmtClassDef) -> (Vec<String>, HashMap<String, (&'a Stmt, &'a StmtFunctionDef)>) {
    let mut order = vec![];
    let mut map = HashMap::new();
    for statement in &node.body {
        if let Stmt::FunctionDef(f) = statement {
            let name = f.name.as_str().to_string();
            if !map.contains_key(&name) {
                order.push(name.clone());
            }
            map.insert(name, (statement, f));
        }
    }
    (order, map)
}

fn positional_after_receiver(f: &StmtFunctionDef) -> Vec<String> {
    f.parameters
        .posonlyargs
        .iter()
        .chain(&f.parameters.args)
        .skip(1)
        .map(|a| a.parameter.name.as_str().to_string())
        .collect()
}

fn name_tests(node: &Node<'_>, parameters: &HashSet<String>) -> Vec<(String, BTreeSet<String>)> {
    let Node::Expr(Expr::Compare(c)) = node else { return vec![] };
    if c.ops.len() != 1 || !matches!(c.ops[0], CmpOp::Eq | CmpOp::NotEq | CmpOp::In | CmpOp::NotIn) {
        return vec![];
    }
    let mut out = vec![];
    for (tested, other) in [(&*c.left, &c.comparators[0]), (&c.comparators[0], &*c.left)] {
        if let Expr::Attribute(a) = tested {
            if let Expr::Name(v) = &*a.value {
                if a.attr.as_str() == "_name" && parameters.contains(v.id.as_str()) {
                    let items: Vec<&Expr> = match other {
                        Expr::Tuple(t) => t.elts.iter().collect(),
                        Expr::List(t) => t.elts.iter().collect(),
                        Expr::Set(t) => t.elts.iter().collect(),
                        o => vec![o],
                    };
                    let models: BTreeSet<String> = items.into_iter().filter_map(text).collect();
                    if !models.is_empty() {
                        out.push((v.id.as_str().to_string(), models));
                    }
                }
            }
        }
    }
    out
}

pub fn record_dispatches(node: &StmtClassDef) -> Vec<(String, String, Vec<String>)> {
    let (order, methods) = methods_map(node);
    let positional: HashMap<&String, Vec<String>> =
        order.iter().map(|n| (n, positional_after_receiver(methods[n].1))).collect();
    let mut tested_order: Vec<(String, String)> = vec![];
    let mut tested: HashMap<(String, String), BTreeSet<String>> = HashMap::new();
    let mut touch = |key: (String, String), tested: &mut HashMap<_, BTreeSet<String>>, order: &mut Vec<_>| {
        if !tested.contains_key(&key) {
            order.push(key.clone());
            tested.insert(key, BTreeSet::new());
        }
    };
    let mut passed: Vec<(String, String, String, String)> = vec![];
    for name in &order {
        let (stmt, f) = methods[name];
        let receiver = f.parameters.args.first().map(|a| a.parameter.name.as_str());
        let mut own: HashSet<String> = positional[name].iter().cloned().collect();
        own.extend(f.parameters.kwonlyargs.iter().map(|a| a.parameter.name.as_str().to_string()));
        for item in nodes_of(stmt) {
            for (parameter, models) in name_tests(&item, &own) {
                let key = (name.clone(), parameter);
                touch(key.clone(), &mut tested, &mut tested_order);
                tested.get_mut(&key).unwrap().extend(models);
            }
            if let Node::Expr(Expr::Call(call)) = item {
                if let Expr::Attribute(fa) = &*call.func {
                    if let Expr::Name(v) = &*fa.value {
                        let callee = fa.attr.as_str();
                        if Some(v.id.as_str()) == receiver && methods.contains_key(callee) {
                            let callee_params = &positional[&callee.to_string()];
                            for (position, argument) in call.arguments.args.iter().enumerate() {
                                if let Expr::Name(a) = argument {
                                    if own.contains(a.id.as_str()) && position < callee_params.len() {
                                        passed.push((name.clone(), a.id.as_str().to_string(), callee.to_string(), callee_params[position].clone()));
                                    }
                                }
                            }
                            for keyword in &call.arguments.keywords {
                                if let (Some(arg), Expr::Name(a)) = (&keyword.arg, &keyword.value) {
                                    if own.contains(a.id.as_str()) {
                                        passed.push((name.clone(), a.id.as_str().to_string(), callee.to_string(), arg.as_str().to_string()));
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    let mut changed = true;
    while changed {
        changed = false;
        for (name, parameter, callee, target) in &passed {
            let found = tested.get(&(callee.clone(), target.clone())).cloned().unwrap_or_default();
            let key = (name.clone(), parameter.clone());
            touch(key.clone(), &mut tested, &mut tested_order);
            let known = tested.get_mut(&key).unwrap();
            if !found.is_subset(known) {
                known.extend(found);
                changed = true;
            }
        }
    }
    tested_order
        .into_iter()
        .filter_map(|key| {
            let models = &tested[&key];
            (!models.is_empty()).then(|| (key.0, key.1, models.iter().cloned().collect()))
        })
        .collect()
}

// ---- passed_containers ----

type Passed = (String, String, Vec<String>, String);

fn names_sequence(annotation: &Expr) -> bool {
    if let Expr::StringLiteral(s) = annotation {
        let t = s.value.to_str();
        return SEQUENCES.iter().any(|n| t.contains(n));
    }
    if matches!(annotation, Expr::BytesLiteral(_) | Expr::NumberLiteral(_) | Expr::BooleanLiteral(_) | Expr::NoneLiteral(_) | Expr::EllipsisLiteral(_)) {
        return false;
    }
    struct Find(bool);
    impl<'a> Visitor<'a> for Find {
        fn visit_expr(&mut self, e: &'a Expr) {
            if let Expr::Name(n) = e {
                if SEQUENCES.contains(&n.id.as_str()) {
                    self.0 = true;
                }
            }
            walk_expr(self, e);
        }
    }
    let mut f = Find(false);
    f.visit_expr(annotation);
    f.0
}

fn parameters_of(f: &StmtFunctionDef) -> Vec<&ruff_python_ast::Parameter> {
    f.parameters
        .posonlyargs
        .iter()
        .chain(&f.parameters.args)
        .skip(1)
        .chain(&f.parameters.kwonlyargs)
        .map(|a| &a.parameter)
        .collect()
}

fn bindings_of<'a>(stmt: &'a Stmt) -> HashMap<String, Vec<&'a Expr>> {
    let mut bound: HashMap<String, Vec<&'a Expr>> = HashMap::new();
    for item in nodes_of(stmt) {
        let (targets, value): (Vec<&Expr>, &Expr) = match item {
            Node::Stmt(Stmt::Assign(a)) => (a.targets.iter().collect(), &a.value),
            Node::Stmt(Stmt::AnnAssign(a)) if a.value.is_some() => (vec![&a.target], a.value.as_deref().unwrap()),
            Node::Expr(Expr::Named(n)) => (vec![&n.target], &n.value),
            _ => continue,
        };
        for target in targets {
            if let Expr::Name(n) = target {
                bound.entry(n.id.as_str().to_string()).or_default().push(value);
            }
        }
    }
    bound
}

fn displays(node: &Expr, bindings: &HashMap<String, Vec<&Expr>>, path: &[String], seen: &BTreeSet<String>, out: &mut Vec<(Vec<String>, &'static str)>) {
    if path.len() > MAX_PATH {
        return;
    }
    match node {
        Expr::Name(n) => {
            if !seen.contains(n.id.as_str()) {
                let mut more = seen.clone();
                more.insert(n.id.as_str().to_string());
                for value in bindings.get(n.id.as_str()).into_iter().flatten() {
                    displays(value, bindings, path, &more, out);
                }
            }
            return;
        }
        Expr::If(i) => {
            displays(&i.body, bindings, path, seen, out);
            displays(&i.orelse, bindings, path, seen, out);
            return;
        }
        _ => {}
    }
    let kind = match node {
        Expr::Dict(_) | Expr::DictComp(_) => "dict",
        Expr::List(_) | Expr::ListComp(_) => "list",
        Expr::Set(_) | Expr::SetComp(_) => "set",
        Expr::Tuple(_) => "tuple",
        _ => return,
    };
    out.push((path.to_vec(), kind));
    let inner: Vec<(String, &Expr)> = match node {
        Expr::Dict(d) => d.items.iter().map(|i| ("v".to_string(), &i.value)).collect(),
        Expr::DictComp(d) => vec![("v".to_string(), &*d.value)],
        Expr::Tuple(t) => {
            if t.elts.iter().any(|e| matches!(e, Expr::Starred(_))) {
                vec![]
            } else {
                t.elts.iter().enumerate().map(|(i, e)| (i.to_string(), e)).collect()
            }
        }
        Expr::List(l) => l.elts.iter().filter(|e| !matches!(e, Expr::Starred(_))).map(|e| ("i".to_string(), e)).collect(),
        Expr::Set(l) => l.elts.iter().filter(|e| !matches!(e, Expr::Starred(_))).map(|e| ("i".to_string(), e)).collect(),
        Expr::ListComp(l) => vec![("i".to_string(), &*l.elt)],
        Expr::SetComp(l) => vec![("i".to_string(), &*l.elt)],
        _ => vec![],
    };
    for (step, value) in inner {
        let mut more = path.to_vec();
        more.push(step);
        displays(value, bindings, &more, seen, out);
    }
}

fn annotated(annotation: &Expr, path: &[String]) -> Option<String> {
    if let Expr::StringLiteral(s) = annotation {
        let source = s.value.to_str().to_string();
        let parsed = ruff_python_parser::parse_expression(&source).ok()?;
        return annotated(parsed.expr(), path);
    }
    match annotation {
        Expr::BinOp(b) if b.op == Operator::BitOr => annotated(&b.left, path).or_else(|| annotated(&b.right, path)),
        Expr::Name(n) => path.is_empty().then(|| n.id.as_str().to_string()),
        Expr::Subscript(s) => {
            let Expr::Name(base) = &*s.value else { return None };
            let name = base.id.as_str();
            if path.is_empty() {
                return Some(name.to_string());
            }
            let items: Vec<&Expr> = match &*s.slice {
                Expr::Tuple(t) => t.elts.iter().collect(),
                other => vec![other],
            };
            let step = path[0].as_str();
            let variadic = items.iter().any(|i| matches!(i, Expr::EllipsisLiteral(_)));
            let inner = if step == "v" {
                (name == "dict" && items.len() == 2).then(|| items[1])
            } else if step == "i" {
                (SEQUENCES.contains(&name) || (name == "tuple" && variadic)).then(|| items[0])
            } else if name == "tuple" && !variadic {
                step.parse::<usize>().ok().filter(|&i| i < items.len()).map(|i| items[i])
            } else {
                None
            };
            inner.and_then(|i| annotated(i, &path[1..]))
        }
        _ => None,
    }
}

pub fn passed_containers(node: &StmtClassDef) -> Vec<Passed> {
    let (order, methods) = methods_map(node);
    let mut annotations: HashMap<&String, Vec<(String, &Expr)>> = HashMap::new();
    for name in &order {
        let f = methods[name].1;
        let found: Vec<(String, &Expr)> = parameters_of(f)
            .into_iter()
            .filter_map(|a| a.annotation.as_deref().filter(|x| names_sequence(x)).map(|x| (a.name.as_str().to_string(), x)))
            .collect();
        // a dict in Python: the last annotation of a name wins (parameter names are unique)
        annotations.insert(name, found);
    }
    if annotations.values().all(|a| a.is_empty()) {
        return vec![];
    }
    let positional: HashMap<&String, Vec<String>> =
        order.iter().map(|n| (n, positional_after_receiver(methods[n].1))).collect();
    let mut found: Ordered<Passed> = Ordered::default();
    for name in &order {
        let (stmt, f) = methods[name];
        let own: HashSet<String> = parameters_of(f).iter().map(|a| a.name.as_str().to_string()).collect();
        let calls: Vec<&ExprCall> = nodes_of(stmt)
            .into_iter()
            .filter_map(|n| match n {
                Node::Expr(Expr::Call(c)) => match &*c.func {
                    Expr::Attribute(a) if annotations.get(&a.attr.as_str().to_string()).is_some_and(|x| !x.is_empty()) => Some(c),
                    _ => None,
                },
                _ => None,
            })
            .collect();
        let bindings = if calls.is_empty() { HashMap::new() } else { bindings_of(stmt) };
        for call in calls {
            let Expr::Attribute(a) = &*call.func else { continue };
            let callee = a.attr.as_str().to_string();
            let annotated_params = &annotations[&callee];
            let callee_positional = &positional[&callee];
            let mut arguments: Vec<(String, &Expr)> = vec![];
            for (position, argument) in call.arguments.args.iter().enumerate() {
                if matches!(argument, Expr::Starred(_)) {
                    break;
                }
                if position < callee_positional.len() {
                    arguments.push((callee_positional[position].clone(), argument));
                }
            }
            for keyword in &call.arguments.keywords {
                if let Some(arg) = &keyword.arg {
                    arguments.push((arg.as_str().to_string(), &keyword.value));
                }
            }
            for (parameter, argument) in arguments {
                let Some((_, annotation)) = annotated_params.iter().find(|(p, _)| *p == parameter) else { continue };
                if let Expr::Name(n) = argument {
                    if own.contains(n.id.as_str()) {
                        found.add((callee.clone(), parameter.clone(), vec!["=".to_string(), name.clone()], n.id.as_str().to_string()));
                        continue;
                    }
                }
                let mut out = vec![];
                displays(argument, &bindings, &[], &BTreeSet::new(), &mut out);
                for (path, kind) in out {
                    let named = annotated(annotation, &path);
                    if named.as_deref().is_some_and(|n| SEQUENCES.contains(&n)) && GIVEN.contains(&kind) && named.as_deref() != Some(kind) {
                        found.add((callee.clone(), parameter.clone(), path, kind.to_string()));
                    }
                }
            }
        }
    }
    found
        .items
        .into_iter()
        .filter(|(callee, parameter, path, kind)| !(path.len() == 2 && path[0] == "=" && &path[1] == callee && kind == parameter))
        .collect()
}
