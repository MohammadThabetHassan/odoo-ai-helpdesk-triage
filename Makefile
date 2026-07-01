ADDONS_PATH ?= addons,.
DB ?= ai_helpdesk_test
ODOO ?= python odoo-bin
MODULE ?= ai_helpdesk_triage

.PHONY: up down test lint eval

up:
	docker compose up -d

down:
	docker compose down

test:
	$(ODOO) -d $(DB) --stop-after-init -i $(MODULE) --test-enable --test-tags /$(MODULE) --addons-path=$(ADDONS_PATH)

lint:
	ruff check ai_helpdesk_triage
	black --check ai_helpdesk_triage

eval:
	python ai_helpdesk_triage/scripts/evaluate.py
