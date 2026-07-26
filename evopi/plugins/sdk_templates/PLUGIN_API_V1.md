# EvoPi PluginAPI v1

An approved Plugin runs as trusted Python code in the EvoPi process. The API
is an extension contract and governance integration point, not an OS sandbox.

`Plugin.register(api)` may contribute:

- Tools and Policies;
- asynchronous or synchronous slash Commands;
- Context Providers and dynamic Prompt Fragments;
- observational lifecycle Event Handlers;
- owner-scoped active Tool restrictions;
- Session Tree-backed namespaced state;
- host-neutral notification, confirmation, selection, input, and status UI.

Execution decisions remain in Policy. Event handlers are observational and
cannot allow, block, confirm, or rewrite a call.

Create candidates with `evopi plugin init`. Never write directly into an
approved artifact snapshot. The activation lifecycle is:

```text
candidate → review → digest-bound approval → reload
```

For substantial extensions, start from a template and use small exact edits.
Keep tests beside the candidate and review again after every source change.
