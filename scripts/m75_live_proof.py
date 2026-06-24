"""M7.5 live proof — SUPERSEDED by scripts/m76_live_proof.py.

M7.6 Job B replaced M7.5's free-text-origin attestation (cross-process corpus breadth) with
connector-VERIFIED directory identity as the provenance root. The original M7.5 proof's model
(origins as bare assignment_group strings, attested by breadth) no longer grants behaviour-rank
trust, so that proof is obsolete. Its scenarios — a genuine multi-origin pattern overriding a
document, and single-origin volume being demoted — are reproduced over VERIFIED identities by
B1/B2 in the consolidated proof.

Run instead:
  OPSFORGE_DATABASE_URL=postgresql+psycopg://opsforge_app:opsforge_app@localhost:5432/opsforge \
  PYTHONPATH=server PYTHONIOENCODING=utf-8 .venv/Scripts/python scripts/m76_live_proof.py
"""

import sys

if __name__ == "__main__":
    sys.stderr.write(__doc__)
    sys.exit("m75_live_proof.py is superseded; run scripts/m76_live_proof.py")
