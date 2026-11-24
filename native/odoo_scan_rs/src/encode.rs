//! Port of `odoo_typing.scan_store.encode` followed by `json.dumps(...,
//! separators=(",", ":"))`: a scan written as the same JSON text, without the encoded
//! objects in between.
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PyString, PyTuple, PyType};
use std::collections::HashMap;

/// `json.dumps` of a string (`ensure_ascii=True`).
pub fn quote(text: &str, out: &mut String) {
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
}

pub struct Encoder<'py> {
    fields_of: Bound<'py, PyAny>,
    source_error: Bound<'py, PyType>,
    path: Bound<'py, PyType>,
    json_dumps: Bound<'py, PyAny>,
    encode: Bound<'py, PyAny>,
    cache: HashMap<usize, Option<Vec<(Bound<'py, PyString>, String)>>>,
}

impl<'py> Encoder<'py> {
    pub fn new(
        fields_of: Bound<'py, PyAny>,
        source_error: Bound<'py, PyType>,
        path: Bound<'py, PyType>,
        json_dumps: Bound<'py, PyAny>,
        encode: Bound<'py, PyAny>,
    ) -> Self {
        Encoder { fields_of, source_error, path, json_dumps, encode, cache: HashMap::new() }
    }

    /// The field names of a dataclass, each with its JSON key text (cached by class).
    fn fields(&mut self, kind: &Bound<'py, PyType>) -> PyResult<Option<Vec<(Bound<'py, PyString>, String)>>> {
        let key = kind.as_ptr() as usize;
        if let Some(found) = self.cache.get(&key) {
            return Ok(found.clone());
        }
        let names = self.fields_of.call1((kind,))?;
        let found = if names.is_none() {
            None
        } else {
            let mut out = Vec::new();
            for name in names.try_iter()? {
                let name = name?.downcast_into::<PyString>()?;
                let mut text = String::new();
                quote(&name.to_cow()?, &mut text);
                out.push((name, text));
            }
            Some(out)
        };
        self.cache.insert(key, found.clone());
        Ok(found)
    }

    /// `json.dumps` of a value as it is (what `json.dumps` writes of what encode gives).
    fn plain(&self, value: &Bound<'py, PyAny>, out: &mut String) -> PyResult<()> {
        if value.is_none() {
            out.push_str("null");
            return Ok(());
        }
        let py = value.py();
        let kind = value.get_type();
        if kind.is(&py.get_type::<PyString>()) {
            match value.downcast::<PyString>()?.to_str() {
                Ok(text) => quote(text, out),
                Err(_) => out.push_str(&self.json_dumps.call1((value,))?.extract::<String>()?),
            }
        } else if kind.is(&py.get_type::<PyBool>()) {
            out.push_str(if value.is_truthy()? { "true" } else { "false" });
        } else if kind.is(&py.get_type::<PyInt>()) {
            match value.extract::<i64>() {
                Ok(small) => out.push_str(&small.to_string()),
                Err(_) => out.push_str(&self.json_dumps.call1((value,))?.extract::<String>()?),
            }
        } else if let Ok(items) = value.downcast::<PyList>() {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                self.plain(&item, out)?;
            }
            out.push(']');
        } else if let Ok(dict) = value.downcast::<PyDict>() {
            out.push('{');
            for (index, (key, item)) in dict.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                let key = key.downcast_into::<PyString>().map_err(|_| PyTypeError::new_err("a key that is not a str"))?;
                quote(&key.to_cow()?, out);
                out.push(':');
                self.plain(&item, out)?;
            }
            out.push('}');
        } else {
            // a float, a subclass of a plain type: json's own text
            out.push_str(&self.json_dumps.call1((value,))?.extract::<String>()?);
        }
        Ok(())
    }

    /// A set: `{"$set": sorted(encode(item) for item in value, key=repr)}`. A set of plain
    /// strings is sorted here (their repr is `'...'`); another is encoded by Python.
    fn set(&mut self, value: &Bound<'py, PyFrozenSet>, out: &mut String) -> PyResult<()> {
        let py = value.py();
        let mut simple: Vec<String> = Vec::with_capacity(value.len());
        for item in value.iter() {
            let plain = item.get_type().is(&py.get_type::<PyString>())
                && item.downcast::<PyString>()?.to_str().is_ok_and(|s| s.bytes().all(|b| (b' '..=b'~').contains(&b) && b != b'\'' && b != b'"' && b != b'\\'));
            if !plain {
                return self.plain(&self.encode.call1((value,))?, out);
            }
            simple.push(item.downcast::<PyString>()?.to_str()?.to_string());
        }
        simple.sort_by(|a, b| format!("'{a}'").cmp(&format!("'{b}'")));
        out.push_str("{\"$set\":[");
        for (index, item) in simple.iter().enumerate() {
            if index > 0 {
                out.push(',');
            }
            quote(item, out);
        }
        out.push_str("]}");
        Ok(())
    }

    pub fn encode(&mut self, value: &Bound<'py, PyAny>, out: &mut String) -> PyResult<()> {
        let py = value.py();
        if value.is_none() {
            out.push_str("null");
            return Ok(());
        }
        let kind = value.get_type();
        if kind.is(&py.get_type::<PyString>()) || kind.is(&py.get_type::<PyInt>()) || kind.is(&py.get_type::<PyBool>()) || kind.is(&py.get_type::<PyFloat>()) {
            return self.plain(value, out);
        }
        if let Some(names) = self.fields(&kind)? {
            out.push_str("{\"$\":");
            quote(&kind.name()?.to_cow()?, out);
            for (name, key) in &names {
                out.push(',');
                out.push_str(key);
                out.push(':');
                self.encode(&value.getattr(name)?, out)?;
            }
            out.push('}');
            return Ok(());
        }
        if value.is_instance(&self.source_error)? {
            out.push_str("{\"$error\":[");
            self.plain(&value.getattr("path")?.str()?.into_any(), out)?;
            out.push(',');
            self.plain(&value.getattr("reason")?, out)?;
            out.push_str("]}");
            return Ok(());
        }
        if let Ok(items) = value.downcast::<PyTuple>() {
            out.push_str("{\"$tuple\":[");
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                self.encode(&item, out)?;
            }
            out.push_str("]}");
            return Ok(());
        }
        if let Ok(set) = value.downcast::<PyFrozenSet>() {
            return self.set(set, out);
        }
        if value.is_instance(&self.path)? {
            out.push_str("{\"$path\":");
            self.plain(&value.str()?.into_any(), out)?;
            out.push('}');
            return Ok(());
        }
        if let Ok(items) = value.downcast::<PyList>() {
            out.push('[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                self.encode(&item, out)?;
            }
            out.push(']');
            return Ok(());
        }
        if let Ok(dict) = value.downcast::<PyDict>() {
            out.push_str("{\"$dict\":[");
            for (index, (key, item)) in dict.iter().enumerate() {
                if index > 0 {
                    out.push(',');
                }
                out.push('[');
                self.encode(&key, out)?;
                out.push(',');
                self.encode(&item, out)?;
                out.push(']');
            }
            out.push_str("]}");
            return Ok(());
        }
        if value.is_instance_of::<PyString>() || value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>() {
            return self.plain(value, out);
        }
        Err(PyTypeError::new_err(format!("cannot store {} in a scan", kind.name()?)))
    }
}
