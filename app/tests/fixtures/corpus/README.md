# Evidence corpus

Offline fixtures for the benchmark in `app/tests/test_evidence_benchmark.py`.
One directory per case: `record.xml` is a PubMed efetch payload, `expected.json`
records what the pipeline must conclude about it.

These are hand-written fixtures modelled on real PubMed responses, not copies of
real records: the point is to exercise the classification and gate paths, and
invented DOIs keep the corpus from ever resolving to a real paper.

Add a case whenever a new failure mode is found in the wild. A corpus that only
contains cases the system already handles stops being a test.
