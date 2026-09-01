.PHONY: run record test test-py test-js test-e2e

run:  ## Serve the app on http://localhost:8765
	PYTHONPATH=server python3 -m micclaude

record:  ## Listen on a microphone and post the text to a running server
	PYTHONPATH=server python3 -m micclaude.recorder

test: test-py test-js  ## Unit tests, no microphone or API key needed

test-py:
	cd tests/python && python3 -m unittest discover -s . -t .

test-js:
	node --test "tests/js/*.test.js"

test-e2e:  ## Drives the real page in Chromium; needs `npm install`
	node --test "tests/e2e/*.test.js"
