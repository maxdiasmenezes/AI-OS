# Engineering Principles

These are the principles that guide how AI-OS is designed and built. When a
decision isn't obvious, default to these.

## 1. Kernel first, capabilities second

Shared problems (memory, knowledge retrieval, tool access, model access) are
solved once in the kernel. Capabilities consume kernel services; they do not
reimplement them.

## 2. Clear boundaries, no leakage

Interfaces don't contain domain logic. The kernel doesn't contain
domain-specific logic. Capabilities don't call each other directly — they
coordinate through the orchestrator. A change in one layer should not force a
change in another.

## 3. Simplicity over abstraction

Build for the capabilities that exist, not the ones that might exist. Prefer
a concrete, working solution for one or two capabilities over a generic
framework built in advance for a dozen. Add abstraction when duplication
actually hurts, not before.

## 4. Data is not code

Prompts, configuration, and persisted state (`prompts/`, `storage/`,
`kernel/config/`) are kept separate from implementation. They should be
editable, reviewable, and versionable without touching application code.

## 5. Memory and knowledge are first-class

An AI employee is only as useful as what it remembers and what it knows.
Memory and knowledge are treated as core kernel responsibilities, not
afterthoughts bolted onto individual capabilities.

## 6. Model-agnostic by design

Capabilities and the orchestrator depend on the kernel's model abstraction,
not on a specific provider or model. Swapping or adding a model should not
require rewriting capability logic.

## 7. Every capability is independently understandable

A new capability should be explainable, testable, and removable on its own,
without needing to understand every other capability first.

## 8. Prefer explicit over implicit

Routing, configuration, and data flow should be traceable and inspectable,
not hidden behind implicit conventions. When in doubt, make the behavior
visible.

## 9. Build incrementally, document as you go

Each layer is built and proven before the next depends on it. Documentation
(`docs/`) is kept up to date with the actual shape of the system, not the
aspirational one.

## 10. Personal tool, production discipline

AI-OS is a personal system, but it is built with the same care as
production software: correctness, security, and maintainability are not
optional just because the audience is one person.

## 11. Human approval for important actions

AI proposes.

Humans approve.

AI-OS must require explicit human approval before executing actions that may
modify data, spend money, communicate externally, or perform irreversible
operations.