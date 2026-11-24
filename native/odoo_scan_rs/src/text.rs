//! Text searches of the registry over addon sources (`visible._defined_names`,
//! `class_index._named`) and the imports of a module (`edges.imported_names`).
use regex::bytes::Regex;
use ruff_python_ast::visitor::Visitor;
use ruff_python_ast::Stmt;
use std::collections::BTreeSet;
use std::path::Path;
use std::sync::OnceLock;

const SKIPPED_DIRS: [&str; 4] = ["static", "i18n", "node_modules", "__pycache__"];
// Python's bytes `\s` and `\w`
const S: &str = r"[ \t\n\r\x0b\x0c]";
const W: &str = r"[A-Za-z0-9_]";

fn definition() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(&format!(r#"(?-u)_name{S}*={S}*(?:{W}+{S}*={S}*)*[rRuU]?['"]([^'"\\\n]+)['"]"#)).unwrap())
}

fn contains(data: &[u8], needle: &[u8]) -> bool {
    data.windows(needle.len()).any(|w| w == needle)
}

fn lossy(bytes: &[u8]) -> String {
    String::from_utf8_lossy(bytes).into_owned()
}

/// `_file_defined_names` of every `.py` file under a directory, walked as `os.walk` does.
pub fn defined_names(directory: &Path) -> BTreeSet<String> {
    let mut names = BTreeSet::new();
    let mut pending = vec![directory.to_path_buf()];
    while let Some(dir) = pending.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else { continue };
        let mut subdirs = vec![];
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().into_owned();
            let path = entry.path();
            let is_dir = std::fs::metadata(&path).map(|m| m.is_dir()).unwrap_or(false);
            if is_dir {
                let link = entry.file_type().map(|t| t.is_symlink()).unwrap_or(false);
                if !SKIPPED_DIRS.contains(&name.as_str()) && !link {
                    subdirs.push(path);
                }
            } else if name.ends_with(".py") {
                if let Ok(data) = std::fs::read(&path) {
                    if contains(&data, b"_name") {
                        for c in definition().captures_iter(&data) {
                            names.insert(lossy(&c[1]));
                        }
                    }
                }
            }
        }
        pending.extend(subdirs);
    }
    names
}

fn class_models() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    // `\b_(?:name|inherit)\s*(?::[^=\n]+)?(\+?)=(?!=)\s*(?:\w+\s*=\s*)*(literal)?`; the
    // `(?!=)` is checked by hand (no look-around here)
    R.get_or_init(|| {
        Regex::new(&format!(
            r#"(?-u)\b_(?:name|inherit){S}*(?::[^=\n]+)?(\+?)(=)"#
        ))
        .unwrap()
    })
}

fn class_models_rest() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| {
        Regex::new(&format!(
            r#"(?-u)\A{S}*(?:{W}+{S}*={S}*)*(\[[^\]]*\]|\([^)]*\)|[rRuU]?['"][^'"\\\n]+['"])?"#
        ))
        .unwrap()
    })
}

fn quoted() -> &'static Regex {
    static R: OnceLock<Regex> = OnceLock::new();
    R.get_or_init(|| Regex::new(r#"(?-u)['"]([^'"\\\n]+)['"]"#).unwrap())
}

/// `class_index._named` on a file's bytes: the models of its `_name` / `_inherit`
/// literals, and whether the scanner must evaluate some (`None`: no such assignment).
pub fn named_literals(data: &[u8]) -> (BTreeSet<String>, bool) {
    let mut names = BTreeSet::new();
    let mut evaluate = false;
    if !contains(data, b"_name") && !contains(data, b"_inherit") {
        return (names, false);
    }
    let mut at = 0;
    while at <= data.len() {
        let Some(c) = class_models().captures_at(data, at) else { break };
        let whole = c.get(0).unwrap();
        let equals = c.get(2).unwrap();
        if data.get(equals.end()) == Some(&b'=') {
            at = whole.start() + 1; // `==`: this start fails, Python tries the next one
            continue;
        }
        let extended = !c[1].is_empty();
        let rest = class_models_rest().captures(&data[equals.end()..]).unwrap();
        let literal = rest.get(1);
        evaluate = evaluate || extended || literal.is_none();
        if let Some(l) = literal {
            for q in quoted().captures_iter(l.as_bytes()) {
                names.insert(lossy(&q[1]));
            }
        }
        let end = equals.end() + rest.get(0).unwrap().end();
        at = if end > whole.start() { end } else { whole.start() + 1 };
    }
    (names, evaluate)
}

/// `edges.imported_names` of a parsed module.
pub fn imported_names(body: &[Stmt], package: &str) -> BTreeSet<String> {
    struct Imports<'p> {
        package: &'p str,
        found: BTreeSet<String>,
    }
    impl<'a> Visitor<'a> for Imports<'_> {
        fn visit_stmt(&mut self, stmt: &'a Stmt) {
            match stmt {
                Stmt::Import(i) => {
                    for alias in &i.names {
                        self.found.insert(alias.name.as_str().to_string());
                    }
                }
                Stmt::ImportFrom(i) => {
                    let parts: Vec<&str> = if self.package.is_empty() { vec![] } else { self.package.split('.').collect() };
                    let level = i.level as isize;
                    let base: Vec<&str> = if level > 0 {
                        let end = parts.len() as isize - (level - 1);
                        let end = if end >= 0 { (end as usize).min(parts.len()) } else { (parts.len() as isize + end).max(0) as usize };
                        parts[..end].to_vec()
                    } else {
                        vec![]
                    };
                    let mut all: Vec<&str> = base;
                    if let Some(m) = &i.module {
                        all.push(m.as_str());
                    }
                    let source = all.join(".");
                    if !source.is_empty() {
                        for alias in &i.names {
                            self.found.insert(format!("{source}.{}", alias.name.as_str()));
                        }
                        self.found.insert(source);
                    }
                }
                _ => {}
            }
            crate::guards::walk_stmt_once(self, stmt);
        }
    }
    let mut v = Imports { package, found: BTreeSet::new() };
    v.visit_body(body);
    v.found
}
