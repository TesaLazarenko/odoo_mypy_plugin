//! Ports of the registry's linearization (`odoo_typing.registry.linearization`): the
//! `LastOrderedSet` of `_registry_bases` and the C3 merge of `graphs.c3_merge`, over
//! keys (a class's index, a model's name) in place of the classes themselves.
use indexmap::IndexSet;
use std::collections::{HashMap, HashSet};

#[derive(Clone, PartialEq, Eq, Hash)]
pub enum Node {
    Class(usize),
    Model(String),
}

/// `Linearization._registry_bases` once the classes are known: `parents_of` are the
/// parents (`_parents`) of each class in registration order, `dropped` the parents that
/// add nothing (on a cycle, or no class in the view).
pub fn registry_bases(model: &str, parents_of: &[Vec<String>], dropped: &HashSet<String>) -> Vec<Node> {
    let mut bases: Vec<Node> = Vec::new();
    for (index, parents) in parents_of.iter().enumerate() {
        let mut new: IndexSet<Node> = IndexSet::new();
        new.insert(Node::Class(index));
        for parent in parents {
            if parent == model {
                for node in &bases {
                    new.shift_remove(node);
                    new.insert(node.clone());
                }
            } else if !dropped.contains(parent) {
                let node = Node::Model(parent.clone());
                new.shift_remove(&node);
                new.insert(node);
            }
        }
        bases = new.into_iter().collect();
    }
    bases
}

/// `graphs.c3_merge`: the merged order and whether a conflict was broken.
pub fn c3_merge(sequences: &[Vec<u32>]) -> (Vec<u32>, bool) {
    let mut lists: Vec<Vec<u32>> =
        sequences.iter().filter(|s| !s.is_empty()).map(|s| s.iter().rev().copied().collect()).collect();
    let mut in_tails: HashMap<u32, i64> = HashMap::new();
    for items in &lists {
        for item in &items[..items.len() - 1] {
            *in_tails.entry(*item).or_insert(0) += 1;
        }
    }
    let mut merged = Vec::new();
    let mut conflict = false;
    while !lists.is_empty() {
        let found = lists.iter().map(|items| *items.last().unwrap()).find(|head| in_tails.get(head).copied().unwrap_or(0) == 0);
        let head = match found {
            Some(head) => head,
            None => {
                conflict = true;
                *lists[0].last().unwrap()
            }
        };
        merged.push(head);
        let in_some_tail = in_tails.get(&head).copied().unwrap_or(0) > 0;
        let mut remaining = Vec::with_capacity(lists.len());
        for mut items in lists {
            if *items.last().unwrap() == head {
                items.pop();
                if let Some(next) = items.last() {
                    *in_tails.get_mut(next).unwrap() -= 1;
                }
            } else if in_some_tail {
                if let Some(at) = items.iter().position(|item| *item == head) {
                    items.remove(at);
                    *in_tails.get_mut(&head).unwrap() -= 1;
                }
            }
            if !items.is_empty() {
                remaining.push(items);
            }
        }
        lists = remaining;
    }
    (merged, conflict)
}
