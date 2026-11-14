# All targets are thin wrappers so they also work without make: run the command on the right.
PY ?= python

.PHONY: seed reset sync-contacts serve sms-sim docs poll poll-once test

seed:      ; $(PY) -m scripts.seed.seed
reset:     ; $(PY) -m scripts.seed.seed --reset
sync-contacts: ; $(PY) -m scripts.seed.seed --sync-contacts
serve:     ; $(PY) -m uvicorn app:app --port 8000 --reload
sms-sim:   ; $(PY) scripts/sms_sim.py
docs:      ; $(PY) data/seed/make_docs.py
poll:      ; $(PY) -m ingest.poller
poll-once: ; $(PY) -m ingest.poller --once
test:      ; $(PY) -m pytest -q
