# All targets are thin wrappers so they also work without make: run the command on the right.
PY ?= python

.PHONY: seed reset serve sms-sim docs test

seed:      ; $(PY) -m scripts.seed.seed
reset:     ; $(PY) -m scripts.seed.seed --reset
serve:     ; $(PY) -m uvicorn ingest.app:app --port 8000 --reload
sms-sim:   ; $(PY) scripts/sms_sim.py
docs:      ; $(PY) data/seed/make_docs.py
test:      ; $(PY) -m pytest -q
