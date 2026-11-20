# Test fixtures

**Synthetic data.** These files copy the *structure* of real responses (RSS feeds,
EDGAR JSON and HTML, filing layouts), but the text is invented for tests and is not real
statements or filings.

Want real samples? Fetch one response per source on your machine (e.g. with
`curl -A "$SEC_USER_AGENT" ...`), save it here, and add a test. Real layouts catch
parser bugs that synthetic ones don't.
