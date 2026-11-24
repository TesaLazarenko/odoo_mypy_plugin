//! Port of `odoo_typing.scan_store._decode`: a stored scan's JSON value back to the
//! scan's objects (tagged tuples, frozensets, paths, dicts and dataclasses).
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyList, PyString, PyTuple};

pub struct Builders<'py> {
    pub classes: Bound<'py, PyDict>,
    pub path: Bound<'py, PyAny>,
    pub error: Bound<'py, PyAny>,
    pub frozen: Bound<'py, PyAny>,
}

fn intern<'py>(py: Python<'py>, s: &Bound<'py, PyString>) -> Bound<'py, PyAny> {
    unsafe {
        let mut ptr = s.clone().into_ptr();
        pyo3::ffi::PyUnicode_InternInPlace(&mut ptr);
        Bound::from_owned_ptr(py, ptr)
    }
}

fn fail(message: String) -> PyErr {
    PyValueError::new_err(message)
}

pub fn decode<'py>(py: Python<'py>, value: &Bound<'py, PyAny>, b: &Builders<'py>, depth: usize) -> PyResult<Bound<'py, PyAny>> {
    if depth > 400 {
        return Err(fail("nested too deeply".into()));
    }
    if let Ok(s) = value.downcast::<PyString>() {
        return Ok(intern(py, s));
    }
    if let Ok(list) = value.downcast::<PyList>() {
        let items: PyResult<Vec<Bound<'py, PyAny>>> = list.iter().map(|item| decode(py, &item, b, depth + 1)).collect();
        return Ok(PyList::new(py, items?)?.into_any());
    }
    let Ok(dict) = value.downcast::<PyDict>() else {
        return Ok(value.clone()); // bool, int, float, None: as they are
    };
    if let Some(name) = dict.get_item("$")? {
        let name_text: String = name.str()?.extract()?;
        let Some(cls) = b.classes.get_item(&name_text)? else {
            return Err(fail(format!("unknown class {name_text:?}")));
        };
        let kwargs = PyDict::new(py);
        for (key, item) in dict.iter() {
            let key = key.downcast::<PyString>()?;
            if key.to_cow()? != "$" {
                kwargs.set_item(intern(py, key), decode(py, &item, b, depth + 1)?)?;
            }
        }
        return cls.call((), Some(&kwargs));
    }
    if dict.len() != 1 {
        return Err(fail(format!("unknown value {}", value.repr()?)));
    }
    let (tag, payload) = dict.iter().next().unwrap();
    let tag: String = tag.extract()?;
    if tag == "$path" {
        if let Ok(text) = payload.downcast::<PyString>() {
            return b.path.call1((text,));
        }
    }
    let Ok(items) = payload.downcast::<PyList>() else {
        return Err(fail(format!("unknown value {tag}: {}", payload.repr()?)));
    };
    match tag.as_str() {
        "$tuple" => {
            let decoded: PyResult<Vec<Bound<'py, PyAny>>> = items.iter().map(|i| decode(py, &i, b, depth + 1)).collect();
            Ok(PyTuple::new(py, decoded?)?.into_any())
        }
        "$set" => {
            let decoded: PyResult<Vec<Bound<'py, PyAny>>> = items.iter().map(|i| decode(py, &i, b, depth + 1)).collect();
            let set = PyFrozenSet::new(py, &decoded?)?;
            b.frozen.call1((set,))
        }
        "$dict" => {
            let out = PyDict::new(py);
            for pair in items.iter() {
                let pair = pair.downcast::<PyList>().map_err(|_| fail(format!("unknown value {tag}")))?;
                if pair.len() != 2 {
                    return Err(fail(format!("unknown value {tag}")));
                }
                out.set_item(decode(py, &pair.get_item(0)?, b, depth + 1)?, decode(py, &pair.get_item(1)?, b, depth + 1)?)?;
            }
            Ok(out.into_any())
        }
        "$error" if items.len() == 2 => {
            let path = b.path.call1((items.get_item(0)?.str()?,))?;
            b.error.call1((path, items.get_item(1)?.str()?))
        }
        _ => Err(fail(format!("unknown tag {tag:?}"))),
    }
}
