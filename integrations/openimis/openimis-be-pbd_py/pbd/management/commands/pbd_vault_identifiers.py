"""``manage.py pbd_vault_identifiers``: move identifiers already stored in openIMIS to the vault.

    manage.py pbd_vault_identifiers --dry-run
    manage.py pbd_vault_identifiers --program cash_assistance --batch 500
    manage.py pbd_vault_identifiers --national-id-field national_id --contact-field email

Each Individual whose ``json_ext`` carries an identifier is proofed in the Identity Vault under
the ``identity_proofing`` purpose with this openIMIS instance's service identity; openIMIS keeps
the placeholder and the person token. Rows already vaulted are skipped, so the command is safe
to rerun. The vault deduplicates by identifier, so an individual registered twice gets one token.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from ...config import current
from ...migration import migrate_individuals


class Command(BaseCommand):
    help = "Vault national identifiers stored in individual.json_ext (one-off migration)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--dry-run", action="store_true", help="report without writing")
        parser.add_argument("--program", default=None, help="catalogue program for identity proofing")
        parser.add_argument("--batch", type=int, default=500, help="rows per database batch")
        parser.add_argument(
            "--national-id-field", default="national_id", help="json_ext key holding the identifier"
        )
        parser.add_argument(
            "--contact-field", default="email", help="json_ext key holding the contact (optional)"
        )
        parser.add_argument(
            "--username", default="Admin", help="existing openIMIS user the saves are attributed to"
        )

    def handle(self, *args: Any, **opts: Any) -> None:
        from core.models import User  # type: ignore[import-not-found]
        from individual.models import Individual  # type: ignore[import-not-found]

        user = User.objects.filter(username=opts["username"]).first()
        if user is None:
            raise CommandError(
                f"openIMIS user '{opts['username']}' not found; pass --username <existing user>"
            )
        cfg = current()
        program = opts["program"] or cfg.mapping.default_program
        fields = {"national_id": opts["national_id_field"], "contact": opts["contact_field"]}
        placeholder = str(cfg.mapping.vault.get("placeholder", "vaulted"))
        token_key = str(cfg.mapping.vault.get("token_field", "input.jsonExt.pbd_person_token")).split(".")[-1]

        def save(ind: Any, new_ext: dict[str, Any]) -> None:
            ind.json_ext = new_ext
            ind.save(user=user)  # openIMIS history models require the acting user

        queryset = Individual.objects.filter(is_deleted=False).order_by("id")
        total = {"scanned": 0, "vaulted": 0, "skipped": 0, "failed": 0}
        batch = int(opts["batch"])
        offset = 0
        while True:
            rows = list(queryset[offset : offset + batch])
            if not rows:
                break
            summary = migrate_individuals(
                rows,
                program=program,
                vault=cfg.control_plane,
                fields=fields,
                placeholder=placeholder,
                token_key=token_key,
                dry_run=bool(opts["dry_run"]),
                save=save,
                log=lambda m: self.stdout.write(m),
            )
            for k, v in summary.items():
                total[k] += v
            offset += batch
        self.stdout.write(
            f"{'DRY RUN ' if opts['dry_run'] else ''}scanned={total['scanned']} vaulted={total['vaulted']} "
            f"skipped={total['skipped']} failed={total['failed']}"
        )
