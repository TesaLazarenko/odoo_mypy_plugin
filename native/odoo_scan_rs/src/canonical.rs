//! Port of `odoo_typing.fingerprint._canonical` followed by `json.dumps(...,
//! separators=(",", ":"))`: the text a scan's digest is taken of, the same characters.
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString, PyTuple, PyType};
use std::collections::HashMap;

enum Canon {
    Raw(String), // null, true, false, a number: its JSON text
    Str(String), // a JSON string, quoted and escaped
    List(Vec<Canon>),
}

/// `json.dumps` of a string (`ensure_ascii=True`).
fn quote(text: &str) -> String {
    let mut out = String::with_capacity(text.len() + 2);
    out.push('"');
    for c in text.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{08}' => out.push_str("\\b"),
            '\u{0c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
        }
    }
    out.push('"');
    out
}

/// `sorted(items, key=json.dumps)`.
fn sorted(items: Vec<Canon>) -> Canon {
    let mut keyed: Vec<(String, Canon)> = items.into_iter().map(|c| (sort_key(&c), c)).collect();
    keyed.sort_by(|a, b| a.0.cmp(&b.0));
    Canon::List(keyed.into_iter().map(|(_, c)| c).collect())
}

fn write(value: &Canon, item: &str, out: &mut String) {
    match value {
        Canon::Raw(text) | Canon::Str(text) => out.push_str(text),
        Canon::List(items) => {
            out.push('[');
            for (index, part) in items.iter().enumerate() {
                if index > 0 {
                    out.push_str(item);
                }
                write(part, item, out);
            }
            out.push(']');
        }
    }
}

/// `json.dumps` with its default separators: the key sets and dicts are sorted by.
fn sort_key(value: &Canon) -> String {
    let mut out = String::new();
    write(value, ", ", &mut out);
    out
}

pub struct Canonical<'py> {
    pub fields_of: Bound<'py, PyAny>,
    pub path: Bound<'py, PyType>,
    pub json_dumps: Bound<'py, PyAny>,
    cache: HashMap<usize, Option<Vec<Bound<'py, PyString>>>>,
}

impl<'py> Canonical<'py> {
    pub fn new(fields_of: Bound<'py, PyAny>, path: Bound<'py, PyType>, json_dumps: Bound<'py, PyAny>) -> Self {
        Canonical { fields_of, path, json_dumps, cache: HashMap::new() }
    }

    fn fields(&mut self, kind: &Bound<'py, PyType>) -> PyResult<Option<Vec<Bound<'py, PyString>>>> {
        let key = kind.as_ptr() as usize;
        if let Some(found) = self.cache.get(&key) {
            return Ok(found.clone());
        }
        let names = self.fields_of.call1((kind,))?;
        let found = if names.is_none() {
            None
        } else {
            let names: Vec<Bound<'py, PyString>> =
                names.try_iter()?.map(|n| n.and_then(|n| Ok(n.downcast_into::<PyString>()?))).collect::<PyResult<_>>()?;
            Some(names)
        };
        self.cache.insert(key, found.clone());
        Ok(found)
    }

    /// A string as `json.dumps` writes it (Python's own for one that is not UTF-8).
    fn string(&self, value: &Bound<'py, PyAny>) -> PyResult<Canon> {
        let text = value.downcast::<PyString>()?;
        Ok(Canon::Str(match text.to_str() {
            Ok(s) => quote(s),
            Err(_) => self.json_dumps.call1((value,))?.extract()?,
        }))
    }

    fn number(&self, value: &Bound<'py, PyAny>) -> PyResult<Canon> {
        // `json.dumps` of an int or float (its own text: big ints, `1e+16`, `NaN`)
        Ok(Canon::Raw(self.json_dumps.call1((value,))?.extract()?))
    }

    fn canonical(&mut self, value: &Bound<'py, PyAny>) -> PyResult<Canon> {
        if value.is_none() {
            return Ok(Canon::Raw("null".into()));
        }
        let kind = value.get_type();
        if kind.is(&value.py().get_type::<PyString>()) {
            return self.string(value);
        }
        if kind.is(&value.py().get_type::<PyBool>()) {
            return Ok(Canon::Raw(if value.is_truthy()? { "true" } else { "false" }.into()));
        }
        if kind.is(&value.py().get_type::<PyInt>()) {
            if let Ok(small) = value.extract::<i64>() {
                return Ok(Canon::Raw(small.to_string()));
            }
            return self.number(value);
        }
        if kind.is(&value.py().get_type::<PyFloat>()) {
            return self.number(value);
        }
        if let Some(names) = self.fields(&kind)? {
            let mut items = vec![Canon::Str(quote(&kind.name()?.to_cow()?))];
            for name in &names {
                items.push(self.canonical(&value.getattr(name)?)?);
            }
            return Ok(Canon::List(items));
        }
        if let Ok(set) = value.downcast::<PyFrozenSet>() {
            return Ok(sorted(set.iter().map(|i| self.canonical(&i)).collect::<PyResult<_>>()?));
        }
        if let Ok(set) = value.downcast::<PySet>() {
            return Ok(sorted(set.iter().map(|i| self.canonical(&i)).collect::<PyResult<_>>()?));
        }
        if let Ok(items) = value.downcast::<PyTuple>() {
            return Ok(Canon::List(items.iter().map(|i| self.canonical(&i)).collect::<PyResult<_>>()?));
        }
        if let Ok(items) = value.downcast::<PyList>() {
            return Ok(Canon::List(items.iter().map(|i| self.canonical(&i)).collect::<PyResult<_>>()?));
        }
        if let Ok(dict) = value.downcast::<PyDict>() {
            let pairs: Vec<(Bound<'py, PyAny>, Bound<'py, PyAny>)> = dict.iter().collect();
            let mut out = Vec::with_capacity(pairs.len());
            for (key, item) in &pairs {
                out.push(Canon::List(vec![self.canonical(key)?, self.canonical(item)?]));
            }
            return Ok(sorted(out));
        }
        if value.is_instance(&self.path)? {
            return self.string(&value.str()?.into_any());
        }
        if value.is_instance_of::<PyString>() {
            return self.string(value);
        }
        if value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>() {
            return self.number(value);
        }
        Err(PyTypeError::new_err(format!("cannot fingerprint {}", kind.name()?)))
    }

    pub fn text(&mut self, value: &Bound<'py, PyAny>) -> PyResult<String> {
        let canon = self.canonical(value)?;
        let mut out = String::new();
        write(&canon, ",", &mut out);
        Ok(out)
    }
}
