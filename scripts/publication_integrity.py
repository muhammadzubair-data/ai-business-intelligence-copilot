"""Dependency-free checks for public portfolio claims."""
from pathlib import Path
import json, re
ROOT=Path(__file__).resolve().parents[1]

def ok(name, cond):
    if not cond: raise AssertionError(name)
    print(f"PASS: {name}")

readme=(ROOT/'README.md').read_text(encoding='utf-8')
metrics=(ROOT/'config/metrics.yaml').read_text(encoding='utf-8')
dev=json.loads((ROOT/'eval/results/benchmark_results.json').read_text())
hold=json.loads((ROOT/'eval/results/holdout_results.json').read_text())
d=dev['summary']['semantic_offline']; h=hold['summary']['semantic_offline']
metric_count=len(re.findall(r'^  [a-zA-Z0-9_]+:\s*$', metrics, flags=re.M))
# YAML also contains non-metric maps, so validate the documented claim by locating the metrics section and next section.
block=metrics.split('\nmetrics:',1)[1].split('\nunsupported_concepts:',1)[0]
metric_count=len(re.findall(r'^  [a-zA-Z0-9_]+:\s*$', block, flags=re.M))
ok('56 governed metrics', metric_count==56)
ok('development benchmark has 137 questions', d['questions']==137)
ok('development result is 100%', d['overall_pass_rate']==100.0)
ok('held-out benchmark has 30 questions', h['questions']==30)
ok('held-out result is 83.3%', h['overall_pass_rate']==83.3)
ok('held-out metric accuracy is 90%', h['metric_questions']['result_accuracy']==90.0)
ok('README uses final GitHub owner', 'github.com/muhammadzubair-data/ai-business-intelligence-copilot.git' in readme)
print('All publication-integrity checks passed.')
