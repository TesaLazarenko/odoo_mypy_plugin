//! Port of `odoo_typing.xmlids._file_ids`: the XML ids of one data file.
use ruff_python_ast::visitor::{walk_expr, Visitor};
use ruff_python_ast::Expr;

const SHORTCUTS: [(&str, &str); 5] = [
    ("template", "ir.ui.view"),
    ("menuitem", "ir.ui.menu"),
    ("act_window", "ir.actions.act_window"),
    ("report", "ir.actions.report"),
    ("asset", "ir.asset"),
];
pub const UNKNOWN_MODEL: &str = "";

pub enum Ids {
    /// Python must read the file (an encoding the port does not read).
    Fallback,
    /// The file cannot be read (`OSError`).
    Unreadable,
    Found(Vec<(String, String)>),
}

fn qualified(xml_id: &str, addon: &str) -> String {
    if xml_id.contains('.') { xml_id.to_string() } else { format!("{addon}.{xml_id}") }
}

pub fn file_ids(path: &str, addon: &str) -> Ids {
    if path.ends_with(".xml") {
        xml_ids(path, addon)
    } else if path.ends_with(".csv") {
        csv_ids(path, addon)
    } else {
        Ids::Found(vec![])
    }
}

fn tag_of(node: &roxmltree::Node) -> String {
    let name = node.tag_name();
    match name.namespace() {
        Some(ns) => format!("{{{ns}}}{}", name.name()),
        None => name.name().to_string(),
    }
}

/// The encoding the XML declaration names, if any (lowercased).
fn declared_encoding(data: &[u8]) -> Option<String> {
    let head = String::from_utf8_lossy(&data[..data.len().min(200)]); // the declaration is ASCII
    let text = head.trim_start();
    if !text.starts_with("<?xml") {
        return None;
    }
    let end = text.find("?>")?;
    let decl = &text[..end];
    let at = decl.find("encoding")?;
    let rest = decl[at + 8..].trim_start().strip_prefix('=')?.trim_start();
    let quote = rest.chars().next()?;
    let rest = &rest[1..];
    let close = rest.find(quote)?;
    Some(rest[..close].to_ascii_lowercase())
}

fn xml_ids(path: &str, addon: &str) -> Ids {
    let data = match std::fs::read(path) {
        Ok(d) => d,
        Err(_) => return Ids::Unreadable,
    };
    if data.starts_with(b"\xff\xfe") || data.starts_with(b"\xfe\xff") {
        return Ids::Fallback; // UTF-16
    }
    if let Some(enc) = declared_encoding(&data) {
        if !["utf-8", "utf8", "us-ascii", "ascii"].contains(&enc.as_str()) {
            return Ids::Fallback;
        }
    }
    let Ok(text) = std::str::from_utf8(&data) else { return Ids::Fallback }; // Python decodes or refuses it
    let text = text.strip_prefix('\u{feff}').unwrap_or(text);
    let options = roxmltree::ParsingOptions { allow_dtd: true, ..Default::default() };
    let Ok(doc) = roxmltree::Document::parse_with_options(text, options) else { return Ids::Found(vec![]) };
    let mut found = vec![];
    for node in doc.root_element().descendants() {
        if !node.is_element() {
            continue;
        }
        let tag = tag_of(&node);
        if tag == "function" && node.attribute("model") == Some("ir.model.data") && node.attribute("name") == Some("_update_xmlids") {
            for inner in node.descendants().filter(|n| n.is_element()) {
                if let Some(source) = inner.attribute("eval") {
                    for name in updated(source) {
                        found.push((qualified(&name, addon), UNKNOWN_MODEL.to_string()));
                    }
                }
            }
            // Python `continue`s: the function's own children are still iterated by
            // root.iter() (they have no id that counts)
            continue;
        }
        let Some(xml_id) = node.attribute("id").filter(|s| !s.is_empty()) else { continue };
        let model = if tag == "record" { node.attribute("model").filter(|m| !m.is_empty()) } else { None };
        let model = model.or_else(|| SHORTCUTS.iter().find(|(t, _)| *t == tag).map(|(_, m)| *m));
        if let Some(model) = model {
            found.push((qualified(xml_id, addon), model.to_string()));
        }
    }
    Ids::Found(found)
}

/// The `'xml_id': '...'` values of the dicts of an `eval` expression, breadth first.
fn updated(source: &str) -> Vec<String> {
    let source = source.trim();
    if source.is_empty() {
        return vec![];
    }
    let Ok(parsed) = ruff_python_parser::parse_expression(source) else { return vec![] };
    struct Dicts<'a> {
        depth: usize,
        seq: usize,
        found: Vec<(usize, usize, &'a ruff_python_ast::ExprDict)>,
    }
    impl<'a> Visitor<'a> for Dicts<'a> {
        fn visit_expr(&mut self, e: &'a Expr) {
            if let Expr::Dict(d) = e {
                self.found.push((self.depth, self.seq, d));
            }
            self.seq += 1;
            self.depth += 1;
            walk_expr(self, e);
            self.depth -= 1;
        }
        fn visit_keyword(&mut self, k: &'a ruff_python_ast::Keyword) {
            self.depth += 1; // Python's `keyword` node is one level more
            self.visit_expr(&k.value);
            self.depth -= 1;
        }
    }
    let mut v = Dicts { depth: 0, seq: 0, found: vec![] };
    v.visit_expr(parsed.expr());
    v.found.sort_by_key(|(d, s, _)| (*d, *s));
    let text = |e: &Expr| if let Expr::StringLiteral(s) = e { Some(s.value.to_str().to_string()) } else { None };
    let mut out = vec![];
    for (_, _, d) in v.found {
        for item in &d.items {
            let key = item.key.as_ref().and_then(text);
            if key.as_deref() == Some("xml_id") {
                if let Some(name) = text(&item.value).filter(|n| !n.is_empty()) {
                    out.push(name);
                }
            }
        }
    }
    out
}

fn csv_ids(path: &str, addon: &str) -> Ids {
    let data = match std::fs::read(path) {
        Ok(d) => d,
        Err(_) => return Ids::Unreadable,
    };
    if std::str::from_utf8(&data).is_err() || data.contains(&0) {
        return Ids::Fallback;
    }
    let model = std::path::Path::new(path).file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
    let mut reader = csv::ReaderBuilder::new().flexible(true).has_headers(true).from_reader(&data[..]);
    let Ok(headers) = reader.headers().cloned() else { return Ids::Fallback };
    let names: Vec<&str> = headers.iter().collect();
    let Some(column) = names.iter().rposition(|h| *h == "id") else { return Ids::Found(vec![]) };
    let mut found = vec![];
    for record in reader.records() {
        let Ok(record) = record else { return Ids::Fallback };
        if record.iter().all(|f| f.is_empty()) && record.len() <= 1 {
            continue; // DictReader skips empty rows
        }
        if let Some(id) = record.get(column).filter(|s| !s.is_empty()) {
            found.push((qualified(id, addon), model.clone()));
        }
    }
    Ids::Found(found)
}
