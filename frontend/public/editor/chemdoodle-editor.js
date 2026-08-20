(function () {
  "use strict";
  var protocol = "tricycle-chemdoodle-editor/1";
  var sketcher = null;
  var api = window.ChemDoodle;
  var ready = false;
  var reactionMode = false;

  function post(message) {
    window.parent.postMessage(Object.assign({ protocol: protocol }, message), "*");
  }

  function molfile() {
    if (!sketcher || !api) return "";
    var molecule = sketcher.getMolecule();
    return molecule && molecule.atoms && molecule.atoms.length ? api.writeMOL(molecule) : "";
  }

  function smiles() {
    // ChemDoodle's SMILES service is remote; the parent converts MOL with RDKit.
    return "";
  }

  function rxn() {
    if (!sketcher || !api || typeof api.writeRXN !== "function") return "";
    // ChemDoodle's RXN writer accepts the molecule and shape collections,
    // rather than the SketcherCanvas instance itself.
    return api.writeRXN(sketcher.molecules, sketcher.shapes);
  }

  function emitChange() {
    if (!ready) return;
    if (reactionMode) post({ type: "reactionChange", rxn: rxn() });
    else post({ type: "change", molfile: molfile(), smiles: smiles() });
  }

  function emitChangeAfterInput() {
    window.setTimeout(emitChange, 0);
  }

  function emitLayout() {
    var canvas = document.getElementById("editor-canvas");
    if (!canvas) return;
    // Body scrollHeight includes the iframe viewport and grows with the parent.
    // The canvas offset plus its own height is the intrinsic editor content size.
    var height = canvas.offsetTop + canvas.offsetHeight;
    post({ type: "layout", height: height });
  }

  function initialize() {
    try {
      if (!api || typeof api.SketcherCanvas !== "function") throw new Error("ChemDoodle SketcherCanvas is unavailable");
      var params = new URLSearchParams(window.location.search);
      reactionMode = params.get("mode") === "reaction";
      var oneMolecule = params.get("oneMolecule") !== "false";
      sketcher = new api.SketcherCanvas("editor-canvas", 320, 260, {
        useServices: false,
        oneMolecule: oneMolecule,
        floatDrawTools: false,
      });
      sketcher.styles.atoms_displayTerminalCarbonLabels_2D = true;
      sketcher.styles.atoms_useJMOLColors = true;
      sketcher.styles.bonds_clearOverlaps_2D = true;
      ready = true;
      post({ type: "ready" });
      window.setTimeout(emitLayout, 0);
      document.addEventListener("pointerup", emitChangeAfterInput);
      document.addEventListener("keyup", emitChange);
      if (typeof ResizeObserver === "function") {
        new ResizeObserver(emitLayout).observe(document.body);
      }
    } catch (error) {
      post({ type: "error", message: error instanceof Error ? error.message : "ChemDoodle editor failed to initialize" });
    }
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent || !event.data || event.data.protocol !== protocol || event.data.type !== "command") return;
    var message = event.data;
    if (!ready || !sketcher || !api || typeof message.command !== "string") return;
    try {
      if (message.command === "loadMolfile" && typeof message.molfile === "string") {
        if (reactionMode) throw new Error("molecule command is unavailable in reaction mode");
        if (!message.molfile) sketcher.clear();
        else sketcher.loadMolecule(api.readMOL(message.molfile));
        sketcher.repaint();
        emitChange();
      } else if (message.command === "loadRxn" && typeof message.rxn === "string") {
        if (!reactionMode || typeof api.readRXN !== "function") throw new Error("reaction command is unavailable");
        if (!message.rxn) sketcher.clear();
        else {
          var content = api.readRXN(message.rxn);
          if (!content || !Array.isArray(content.molecules)) throw new Error("reaction could not be loaded");
          sketcher.loadContent(content.molecules, content.shapes || []);
        }
        sketcher.repaint();
        emitChange();
      } else if (message.command === "clear") {
        sketcher.clear();
        emitChange();
      } else if (message.command === "resize" && Number.isFinite(message.width) && Number.isFinite(message.height)) {
        sketcher.resize(Math.max(240, message.width), Math.max(120, message.height));
        sketcher.repaint();
        emitLayout();
      } else if (message.command === "getMolfile" || message.command === "getSmiles" || message.command === "getRxn") {
        var value = message.command === "getMolfile" ? molfile() : message.command === "getSmiles" ? smiles() : rxn();
        post({ type: "response", requestId: message.requestId, value: value });
      }
    } catch (error) {
      post({ type: "error", message: error instanceof Error ? error.message : "ChemDoodle editor command failed" });
    }
  });

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", initialize);
  else initialize();
}());
