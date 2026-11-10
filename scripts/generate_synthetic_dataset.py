"""Thin wrapper: python scripts/generate_synthetic_dataset.py --out data/sample/mq_topology_sample.csv"""
import pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from mqresolver.synth import main  # noqa: E402

main()
