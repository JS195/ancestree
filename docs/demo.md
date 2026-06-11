---
hide:
  - toc
---

# Live Demo

This is a real, unmodified `interactive_pipeline.html` produced by the [Machine Learning Pipeline example](examples/ML_pipeline.ipynb) — the iris dataset run through two scalers, three embedding methods, and two clustering algorithms, tracked as 21 nodes across four generations.

Explore it exactly as you would on your own machine: drag to pan, scroll to zoom, and **click any node** to inspect its metadata — hyperparameters, scores, the cluster plots rendered inline as figures, and links to every artifact the node produced.

[:material-open-in-new: Open full screen](assets/demo/interactive_pipeline.html){ .md-button target="_blank" }

<iframe src="../assets/demo/interactive_pipeline.html"
        width="100%" height="720"
        style="border: 1px solid var(--md-default-fg-color--lightest); border-radius: 4px;"
        title="Ancestree interactive pipeline demo"></iframe>

!!! tip "Generate your own"
    This file is the output of a single call — `store.generate_web_graph()` — and is fully self-contained: no server, no dependencies, just open it in a browser or share it as-is.
