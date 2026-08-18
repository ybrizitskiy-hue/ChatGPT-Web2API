"""ChatGPT-Web2API — OpenAI-compatible proxy through ChatGPT web via CDP."""

__version__ = "0.2.0"

# Install narrow production recoveries before callers import the service
# classes. The installers are idempotent and reconcile ambiguous browser/CDP
# outcomes against observable page state instead of blindly retrying actions.
from .runtime_hotfixes import install_runtime_hotfixes as _install_runtime_hotfixes

_install_runtime_hotfixes()
del _install_runtime_hotfixes

from .input_recovery import install_input_recovery as _install_input_recovery

_install_input_recovery()
del _install_input_recovery
