"""SHACL-backed frozen subset: shapes parse, ontology lineage, exact errors."""

from __future__ import annotations

import pytest

from agentcrash import odrl
from agentcrash.odrl import contains_attacker_grant, validate_odrl_subset

CTX = "http://www.w3.org/ns/odrl.jsonld"


def _doc(**overrides):
    base = {
        "@context": CTX,
        "type": "Offer",
        "uid": "urn:odrl:policy:1",
        "permission": [{"action": "use", "target": "urn:data:1"}],
    }
    base.update(overrides)
    return base


def test_shapes_graph_parses_with_expected_shapes() -> None:
    import rdflib

    shapes = set(
        odrl._SHAPES_GRAPH.subjects(
            rdflib.RDF.type, rdflib.URIRef("http://www.w3.org/ns/shacl#NodeShape")
        )
    )
    names = {str(s).split(":")[-1] for s in shapes}
    assert {"PolicyShape", "RuleShape", "AgreementShape"} <= names


def test_ontology_covers_frozen_types_and_classic_actions() -> None:
    import rdflib

    ODRL = rdflib.Namespace("http://www.w3.org/ns/odrl/2/")
    subs = {
        str(s).rsplit("/", 1)[-1]
        for s in odrl._ONTOLOGY_GRAPH.subjects(rdflib.RDFS.subClassOf, ODRL.Policy)
    }
    assert set(odrl.VALID_TYPES) <= subs
    actions = {
        str(s).rsplit("/", 1)[-1]
        for s in odrl._ONTOLOGY_GRAPH.subjects(rdflib.RDF.type, ODRL.Action)
    }
    assert {"use", "reproduce", "distribute", "display", "present"} <= actions


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        ("nope", ["odrl document must be an object"]),
        (
            {},
            [
                f"@context must be {CTX!r}",
                "type must be one of ['Agreement', 'Offer', 'Set']",
                "uid must be a non-empty string",
                "permission must be a non-empty list",
            ],
        ),
        (
            _doc(**{"@context": "http://example.com/other"}),
            [
                "@context must be 'http://www.w3.org/ns/odrl.jsonld'",
            ],
        ),
        (_doc(type="License"), ["type must be one of ['Agreement', 'Offer', 'Set']"]),
        (_doc(type=None), ["type must be one of ['Agreement', 'Offer', 'Set']"]),
        (_doc(uid=""), ["uid must be a non-empty string"]),
        (_doc(uid=5), ["uid must be a non-empty string"]),
        (_doc(permission=[]), ["permission must be a non-empty list"]),
        (_doc(permission="use"), ["permission must be a non-empty list"]),
        (
            _doc(permission=[{"target": "urn:data:1"}]),
            [
                "permission[0] is missing required 'action'",
            ],
        ),
        (
            _doc(permission=[{"action": "use"}]),
            [
                "permission[0] is missing required 'target'",
            ],
        ),
        (
            _doc(permission=[{"action": "", "target": "urn:data:1"}]),
            [
                "permission[0] is missing required 'action'",
            ],
        ),
        (_doc(permission=["use"]), ["permission[0] must be an object"]),
        (
            _doc(permission=[{"action": "use"}, {"target": "urn:data:2"}]),
            [
                "permission[0] is missing required 'target'",
                "permission[1] is missing required 'action'",
            ],
        ),
        (
            _doc(permission=[{"action": "use", "target": "t", "assignee": ""}]),
            ["permission[0] assignee must be a non-empty string"],
        ),
        (
            _doc(type="Agreement", assigner=""),
            ["Agreement requires a non-empty 'assigner'"],
        ),
        (
            _doc(type="Agreement"),
            ["Agreement requires a non-empty 'assigner'"],
        ),
        (
            _doc(type="Offer", permission=[{}]),
            [
                "permission[0] is missing required 'action'",
                "permission[0] is missing required 'target'",
            ],
        ),
    ],
)
def test_exact_legacy_messages(doc, expected) -> None:
    valid, errors = validate_odrl_subset(doc)
    assert valid is (not expected)
    assert errors == expected


def test_valid_docs_pass() -> None:
    valid, errors = validate_odrl_subset(_doc())
    assert valid and errors == []
    strict = _doc(
        type="Agreement",
        assigner="data-steward",
        permission=[{"action": "use", "target": "urn:data:7", "assignee": "team"}],
    )
    valid, errors = validate_odrl_subset(strict)
    assert valid and errors == []


def test_attacker_scan_unchanged() -> None:
    assert contains_attacker_grant({"nested": ["attacker-client"]})
    assert not contains_attacker_grant({"assignee": "research-team"})
