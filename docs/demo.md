---
hide:
  - toc
---

# Live Demo

An unmodified `interactive_pipeline.html` from the [Machine Learning Pipeline example](examples/ml_pipeline.ipynb): the iris dataset run through two scalers, three embedding methods and two clustering algorithms, tracked as 21 nodes across four generations.

It behaves the same here as on your own machine. Drag to pan, scroll to zoom, and click a node for its metadata, including hyperparameters, scores, cluster plots rendered inline, and links to every artifact.

[:material-open-in-new: Open full screen](assets/demo/interactive_pipeline.html){ .md-button target="_blank" }

<iframe src="../assets/demo/interactive_pipeline.html"
        width="100%" height="720"
        style="border: 1px solid var(--md-default-fg-color--lightest); border-radius: 4px;"
        title="Ancestree interactive pipeline demo"></iframe>

!!! tip "Generate your own"
    This file is the output of a single `store.generate_web_graph()` call and is fully self-contained. Open it in a browser or share it as-is.
