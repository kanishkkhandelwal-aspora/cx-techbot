.PHONY: run install test clean

install:
	pip install -r requirements.txt

run:
	python main.py

test:
	python -m pytest tests/ -v

clean:
	rm -f cxbot_metrics.db .cxbot_cursor cxbot_assigner_state.json
