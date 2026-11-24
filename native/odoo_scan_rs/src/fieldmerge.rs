//! Port of `odoo_typing.model_fields.merge_definitions`: the field the definitions of one
//! name make, base first, built as a `FieldInfo` only where it differs from the tip.
use indexmap::IndexMap;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyFrozenSet, PyString, PyTuple};
use std::collections::{BTreeSet, HashMap, HashSet};

#[derive(Clone, PartialEq, Default)]
struct F {
    name: String,
    field_type: String,
    comodel: Option<String>,
    inverse_name: Option<String>,
    selection: Option<Vec<String>>,
    selection_method: Option<String>,
    selection_add: Option<Vec<String>>,
    related: Option<String>,
    compute: Option<String>,
    inverse: Option<String>,
    search: Option<String>,
    readonly: Option<bool>,
    required: Option<bool>,
    store: Option<bool>,
    delegate: Option<bool>,
    compute_sql: Option<bool>,
    attachment: Option<bool>,
    dynamic: BTreeSet<String>,
    given: Vec<String>,
}

pub struct Rules {
    pub parents: HashMap<String, String>,
    pub overridable: Vec<String>,
    pub any_argument: String,
}

/// An exact `str` (a subclass, any other value: `None`, read by Python).
fn text(value: &Bound<'_, PyAny>) -> Option<String> {
    if value.get_type().is(&value.py().get_type::<PyString>()) {
        value.downcast::<PyString>().ok()?.to_str().ok().map(String::from)
    } else {
        None
    }
}

fn opt_text(value: Bound<'_, PyAny>) -> Option<Option<String>> {
    if value.is_none() {
        Some(None)
    } else {
        text(&value).map(Some)
    }
}

fn opt_bool(value: Bound<'_, PyAny>) -> Option<Option<bool>> {
    if value.is_none() {
        return Some(None);
    }
    if value.get_type().is(&value.py().get_type::<pyo3::types::PyBool>()) {
        value.extract::<bool>().ok().map(Some)
    } else {
        None
    }
}

fn texts(value: &Bound<'_, PyAny>) -> Option<Vec<String>> {
    let items = value.downcast::<PyTuple>().ok()?;
    items.iter().map(|item| text(&item)).collect()
}

fn opt_texts(value: Bound<'_, PyAny>) -> Option<Option<Vec<String>>> {
    if value.is_none() {
        Some(None)
    } else {
        texts(&value).map(Some)
    }
}

/// A definition's arguments; `None` for one with a value of another type than the scan
/// gives (Python merges it).
fn read(info: &Bound<'_, PyAny>) -> Option<F> {
    let get = |name: &str| info.getattr(name).ok();
    let dynamic = get("dynamic")?;
    let dynamic = dynamic.downcast::<PyFrozenSet>().ok()?;
    Some(F {
        name: text(&get("name")?)?,
        field_type: text(&get("field_type")?)?,
        comodel: opt_text(get("comodel")?)?,
        inverse_name: opt_text(get("inverse_name")?)?,
        selection: opt_texts(get("selection")?)?,
        selection_method: opt_text(get("selection_method")?)?,
        selection_add: opt_texts(get("selection_add")?)?,
        related: opt_text(get("related")?)?,
        compute: opt_text(get("compute")?)?,
        inverse: opt_text(get("inverse")?)?,
        search: opt_text(get("search")?)?,
        readonly: opt_bool(get("readonly")?)?,
        required: opt_bool(get("required")?)?,
        store: opt_bool(get("store")?)?,
        delegate: opt_bool(get("delegate")?)?,
        compute_sql: opt_bool(get("compute_sql")?)?,
        attachment: opt_bool(get("attachment")?)?,
        dynamic: dynamic.iter().map(|item| text(&item)).collect::<Option<_>>()?,
        given: texts(&get("given")?)?,
    })
}

fn compatible(rules: &Rules, field_type: &str, base_type: &str) -> bool {
    let mut current = field_type;
    while current != base_type {
        match rules.parents.get(current) {
            Some(parent) => current = parent,
            None => break,
        }
    }
    current == base_type
}

/// Odoo's `merge_sequences` (`model_fields.merge_keys`).
fn merge_keys(sequences: &[&[String]]) -> Vec<String> {
    let mut before: IndexMap<&str, Vec<&str>> = IndexMap::new();
    for sequence in sequences {
        let mut previous: Option<&str> = None;
        for key in sequence.iter() {
            let entry = before.entry(key.as_str()).or_default();
            if let Some(previous) = previous {
                entry.push(previous);
            }
            previous = Some(key.as_str());
        }
    }
    fn visit<'k>(key: &'k str, before: &IndexMap<&'k str, Vec<&'k str>>, visited: &mut HashSet<&'k str>, out: &mut Vec<String>) {
        if visited.insert(key) {
            for earlier in &before[key] {
                visit(earlier, before, visited, out);
            }
            out.push(key.to_string());
        }
    }
    let mut out = Vec::new();
    let mut visited = HashSet::new();
    for key in before.keys() {
        visit(key, &before, &mut visited, &mut out);
    }
    out
}

fn merged_given(rules: &Rules, earlier: &[String], later: &[String]) -> Vec<String> {
    let any = rules.any_argument.as_str();
    if earlier.is_empty() || later.iter().any(|g| g == any) || earlier.iter().any(|g| g == any) {
        return later.to_vec();
    }
    if later.is_empty() {
        return earlier.to_vec();
    }
    let mut merged: IndexMap<&str, &String> = IndexMap::new();
    for item in earlier.iter().chain(later) {
        let key = item.split_once('=').map(|(k, _)| k).unwrap_or(item.as_str());
        merged.insert(key, item);
    }
    let mut values: Vec<String> = merged.into_values().cloned().collect();
    values.sort();
    values
}

fn override_(rules: &Rules, merged: Option<F>, d: &F) -> F {
    let m = merged.unwrap_or_else(|| F { name: d.name.clone(), field_type: d.field_type.clone(), ..F::default() });
    let values: [(&str, bool); 12] = [
        ("comodel", d.comodel.is_some()),
        ("inverse_name", d.inverse_name.is_some()),
        ("related", d.related.is_some()),
        ("compute", d.compute.is_some()),
        ("inverse", d.inverse.is_some()),
        ("search", d.search.is_some()),
        ("readonly", d.readonly.is_some()),
        ("required", d.required.is_some()),
        ("store", d.store.is_some()),
        ("delegate", d.delegate.is_some()),
        ("compute_sql", d.compute_sql.is_some()),
        ("attachment", d.attachment.is_some()),
    ];
    let mut passed: HashSet<String> = values.iter().filter(|(_, set)| *set).map(|(n, _)| n.to_string()).collect();
    for name in &d.dynamic {
        if rules.overridable.contains(name) {
            passed.insert(name.clone());
        }
    }
    let mut dynamic: BTreeSet<String> = m.dynamic.iter().filter(|n| !passed.contains(*n)).cloned().collect();
    dynamic.extend(d.dynamic.iter().filter(|n| passed.contains(*n)).cloned());
    fn take<T: Clone>(passed: &HashSet<String>, old: &Option<T>, new: &Option<T>, name: &str) -> Option<T> {
        if passed.contains(name) {
            new.clone()
        } else {
            old.clone()
        }
    }
    let (mut selection, mut method) = (m.selection.clone(), m.selection_method.clone());
    if d.selection.is_some() || d.selection_method.is_some() || d.dynamic.contains("selection") {
        selection = d.selection.clone();
        method = d.selection_method.clone();
        dynamic.remove("selection");
        if d.dynamic.contains("selection") {
            dynamic.insert("selection".into());
        }
    }
    match (&d.selection_add, &selection) {
        (Some(add), Some(keys)) => selection = Some(merge_keys(&[keys, add])),
        _ if d.selection_add.is_some() || d.dynamic.contains("selection_add") => {
            selection = None;
            dynamic.insert("selection".into());
        }
        _ => {}
    }
    dynamic.remove("selection_add");
    F {
        name: m.name.clone(),
        field_type: d.field_type.clone(),
        comodel: take(&passed, &m.comodel, &d.comodel, "comodel"),
        inverse_name: take(&passed, &m.inverse_name, &d.inverse_name, "inverse_name"),
        selection,
        selection_method: method,
        selection_add: None,
        related: take(&passed, &m.related, &d.related, "related"),
        compute: take(&passed, &m.compute, &d.compute, "compute"),
        inverse: take(&passed, &m.inverse, &d.inverse, "inverse"),
        search: take(&passed, &m.search, &d.search, "search"),
        readonly: take(&passed, &m.readonly, &d.readonly, "readonly"),
        required: take(&passed, &m.required, &d.required, "required"),
        store: take(&passed, &m.store, &d.store, "store"),
        delegate: take(&passed, &m.delegate, &d.delegate, "delegate"),
        compute_sql: take(&passed, &m.compute_sql, &d.compute_sql, "compute_sql"),
        attachment: take(&passed, &m.attachment, &d.attachment, "attachment"),
        dynamic,
        given: merged_given(rules, &m.given, &d.given),
    }
}

/// `merge_definitions` of one name: the tip itself where the merge gives it back.
pub fn merge<'py>(
    rules: &Rules,
    definitions: &[Bound<'py, PyAny>],
    field_info: &Bound<'py, PyAny>,
    frozen: &Bound<'py, PyAny>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    let Some(read_all) = definitions.iter().map(read).collect::<Option<Vec<F>>>() else { return Ok(None) };
    let Some(tip) = read_all.last() else { return Ok(None) };
    let mut merged: Option<F> = None;
    for definition in &read_all {
        if !compatible(rules, &tip.field_type, &definition.field_type) {
            merged = None;
            continue;
        }
        merged = Some(override_(rules, merged, definition));
    }
    let Some(mut merged) = merged else { return Ok(None) };
    merged.field_type = tip.field_type.clone();
    merged.selection_add = None;
    let tip_object = definitions.last().unwrap();
    if tip_object.get_type().is(field_info) && merged == *tip {
        return Ok(Some(tip_object.clone()));
    }
    let py = tip_object.py();
    let kwargs = PyDict::new(py);
    kwargs.set_item("comodel", &merged.comodel)?;
    kwargs.set_item("inverse_name", &merged.inverse_name)?;
    kwargs.set_item("selection", merged.selection.as_ref().map(|s| PyTuple::new(py, s)).transpose()?)?;
    kwargs.set_item("selection_method", &merged.selection_method)?;
    kwargs.set_item("related", &merged.related)?;
    kwargs.set_item("compute", &merged.compute)?;
    kwargs.set_item("inverse", &merged.inverse)?;
    kwargs.set_item("search", &merged.search)?;
    kwargs.set_item("readonly", merged.readonly)?;
    kwargs.set_item("required", merged.required)?;
    kwargs.set_item("store", merged.store)?;
    kwargs.set_item("delegate", merged.delegate)?;
    kwargs.set_item("compute_sql", merged.compute_sql)?;
    kwargs.set_item("attachment", merged.attachment)?;
    kwargs.set_item("dynamic", frozen.call1((PyFrozenSet::new(py, &merged.dynamic)?,))?)?;
    kwargs.set_item("given", PyTuple::new(py, &merged.given)?)?;
    Ok(Some(field_info.call((merged.name, merged.field_type), Some(&kwargs))?))
}
