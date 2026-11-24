//! Port of `odoo_typing.scanner.evaluation`: values of constant expressions.
use indexmap::IndexMap;
use ruff_python_ast::{Expr, FStringPart, InterpolatedStringElement, Number, Operator};
use std::collections::HashMap;

#[derive(Clone, Debug, PartialEq)]
pub enum V {
    Unknown,
    Str(String),
    Int(Option<i64>), // `None`: too big for i64 (non-zero)
    Float(f64),
    Complex,
    Bool(bool),
    None,
    Bytes(usize), // length
    Ellipsis,
    List(Vec<V>),
    Tuple(Vec<V>),
    Dict(IndexMap<String, V>),
}

impl V {
    pub fn truthy(&self) -> bool {
        match self {
            V::Unknown | V::Ellipsis | V::Complex => true,
            V::Str(s) => !s.is_empty(),
            V::Int(i) => *i != Some(0),
            V::Float(f) => *f != 0.0,
            V::Bool(b) => *b,
            V::None => false,
            V::Bytes(n) => *n > 0,
            V::List(x) | V::Tuple(x) => !x.is_empty(),
            V::Dict(d) => !d.is_empty(),
        }
    }
    pub fn as_str(&self) -> Option<&str> {
        if let V::Str(s) = self { Some(s) } else { None }
    }
    pub fn items(&self) -> Option<&Vec<V>> {
        match self {
            V::List(x) | V::Tuple(x) => Some(x),
            _ => None,
        }
    }
}

/// Names bound in a scope: a class body's own names, then the module's.
pub trait Scope {
    fn get(&self, name: &str) -> Option<V>;
}

impl Scope for HashMap<String, V> {
    fn get(&self, name: &str) -> Option<V> {
        HashMap::get(self, name).cloned()
    }
}

pub struct Chain<'a> {
    pub first: &'a dyn Scope,
    pub second: &'a dyn Scope,
}

impl Scope for Chain<'_> {
    fn get(&self, name: &str) -> Option<V> {
        self.first.get(name).or_else(|| self.second.get(name))
    }
}

impl Scope for IndexMap<String, V> {
    fn get(&self, name: &str) -> Option<V> {
        IndexMap::get(self, name).cloned()
    }
}

pub fn constant(node: &Expr) -> Option<V> {
    Some(match node {
        Expr::StringLiteral(s) => V::Str(s.value.to_str().to_string()),
        Expr::BytesLiteral(b) => V::Bytes(b.value.bytes().count()),
        Expr::NumberLiteral(n) => match &n.value {
            Number::Int(i) => V::Int(i.as_i64()),
            Number::Float(f) => V::Float(*f),
            Number::Complex { .. } => V::Complex,
        },
        Expr::BooleanLiteral(b) => V::Bool(b.value),
        Expr::NoneLiteral(_) => V::None,
        Expr::EllipsisLiteral(_) => V::Ellipsis,
        _ => return None,
    })
}

pub fn evaluate(node: &Expr, scope: &dyn Scope) -> V {
    if let Some(c) = constant(node) {
        return c;
    }
    match node {
        Expr::Dict(d) => {
            let mut keys = vec![];
            for item in &d.items {
                match item.key.as_ref().map(|k| evaluate(k, scope)) {
                    Some(V::Str(k)) => keys.push(k),
                    _ => return V::Unknown,
                }
            }
            let mut out = IndexMap::new();
            for (k, item) in keys.into_iter().zip(&d.items) {
                out.insert(k, evaluate(&item.value, scope));
            }
            V::Dict(out)
        }
        Expr::Name(n) => scope.get(n.id.as_str()).unwrap_or(V::Unknown),
        Expr::List(_) | Expr::Tuple(_) => {
            let elts = match node {
                Expr::List(l) => &l.elts,
                Expr::Tuple(t) => &t.elts,
                _ => unreachable!(),
            };
            let mut items = vec![];
            for item in elts {
                if let Expr::Starred(s) = item {
                    match spliced(&s.value, scope) {
                        Some(x) => items.extend(x),
                        None => items.push(V::Unknown),
                    }
                } else {
                    items.push(evaluate(item, scope));
                }
            }
            if matches!(node, Expr::List(_)) { V::List(items) } else { V::Tuple(items) }
        }
        Expr::BinOp(b) if b.op == Operator::Add => {
            match (evaluate(&b.left, scope), evaluate(&b.right, scope)) {
                (V::Str(a), V::Str(c)) => V::Str(a + &c),
                (V::List(mut a), V::List(c)) => {
                    a.extend(c);
                    V::List(a)
                }
                (V::Tuple(mut a), V::Tuple(c)) => {
                    a.extend(c);
                    V::Tuple(a)
                }
                _ => V::Unknown,
            }
        }
        Expr::FString(f) => {
            let mut out = String::new();
            for part in &f.value {
                match part {
                    FStringPart::Literal(l) => out.push_str(&l.value),
                    FStringPart::FString(fs) => {
                        for element in &fs.elements {
                            match element {
                                InterpolatedStringElement::Literal(l) => out.push_str(&l.value),
                                InterpolatedStringElement::Interpolation(i) => {
                                    if i.debug_text.is_some()
                                        || i.conversion != ruff_python_ast::ConversionFlag::None
                                        || i.format_spec.is_some()
                                    {
                                        return V::Unknown;
                                    }
                                    match evaluate(&i.expression, scope) {
                                        V::Str(s) => out.push_str(&s),
                                        _ => return V::Unknown,
                                    }
                                }
                            }
                        }
                    }
                }
            }
            V::Str(out)
        }
        _ => V::Unknown,
    }
}

fn spliced(node: &Expr, scope: &dyn Scope) -> Option<Vec<V>> {
    if let Expr::Call(c) = node {
        if let Expr::Attribute(f) = &*c.func {
            if c.arguments.args.is_empty()
                && c.arguments.keywords.is_empty()
                && ["items", "keys", "values"].contains(&f.attr.as_str())
            {
                let V::Dict(known) = evaluate(&f.value, scope) else { return None };
                if known.values().any(|v| *v == V::Unknown) {
                    return None;
                }
                return Some(match f.attr.as_str() {
                    "items" => known.into_iter().map(|(k, v)| V::Tuple(vec![V::Str(k), v])).collect(),
                    "keys" => known.into_keys().map(V::Str).collect(),
                    _ => known.into_values().collect(),
                });
            }
        }
    }
    match evaluate(node, scope) {
        V::List(x) | V::Tuple(x) => Some(x),
        _ => None,
    }
}
