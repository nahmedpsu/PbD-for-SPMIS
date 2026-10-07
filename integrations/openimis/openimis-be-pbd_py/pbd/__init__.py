"""openimis-be-pbd: Privacy-by-Design enforcement module for the openIMIS backend.

Install it like any other openIMIS backend module (add ``openimis-be-pbd`` to ``openimis.json``)
and add ``pbd.middleware.PrivacyMiddleware`` to the graphene middleware list. Every GraphQL
resolution of a mapped entity (Individual, Beneficiary, Group) is then subject to a purpose-bound
decision from the PbD-SPMIS control plane, sensitive fields are released only in the mode the
policy prescribes, every read is audited, and, when enabled, national identifiers are moved to
the Identity Vault at creation time.

The module depends on openIMIS core only through optional, duck-typed hooks so that it can be
tested without a running openIMIS.
"""

default_app_config = "pbd.apps.PbdConfig"
