"""Pure harness logic: phase machine, grading, classification, models, errors.

`core` imports nothing from `runtime`. It receives a ContainerRuntime protocol
implementation as a parameter, which is what keeps phase ordering, grading,
force-restore, and path-jail logic unit-testable without Docker.
"""
