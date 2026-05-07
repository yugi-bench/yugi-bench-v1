"""yugi-bench engine — drives the real EDOPro / ocgcore engine.

Submodules:
  - ``engine.core``          libocgcore FFI, CardDB, OCGEngine
  - ``engine.harness``       20 ``respond_*`` methods, StepResult, PendingDecision
  - ``engine.state``         Perspective-filtered observation builder
  - ``engine.tools``         JSON-Schema tool registry + arg coercion
  - ``engine.episode``       Interactive Episode loop (fully interactive mode)
  - ``engine.replay``        N-attempts bulk replay evaluator (single-attempt core)
                              + ``_auto_advance_opponent`` passive policy
  - ``engine.multi_attempt`` N-attempts retry-on-failure wrapper (N>1)

The unified ``runner.py`` CLI drives both release modes against any
``providers.ToolCallingProvider``.
"""
