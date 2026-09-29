// Injected into every frame during a run. Reports what a human does in the page to Python through
// the __cuaCapture binding. Python decides what to keep (only events inside the human's control
// window) and redacts values before anything is written. Password values are never sent at all.
(() => {
  if (window.__cuaCaptureInstalled) return;
  window.__cuaCaptureInstalled = true;

  const clean = (s) => (s || "").replace(/\s+/g, " ").trim();
  const strip = (s) => clean(s).replace(/:$/, "").trim();
  const report = (payload) => {
    if (typeof window.__cuaCapture === "function") window.__cuaCapture({ at: Date.now(), ...payload });
  };

  // Same vocabulary as the observation: role, accessible-ish name, caption label, form field name.
  function describe(el) {
    const tag = el.tagName.toLowerCase();
    let role = el.getAttribute("role") || { a: "link", button: "button", select: "combobox", textarea: "textbox" }[tag] || tag;
    if (tag === "input") {
      role = { submit: "button", button: "button", reset: "button", checkbox: "checkbox", radio: "radio" }[el.type] || "textbox";
    }
    const isButtonInput = tag === "input" && ["submit", "button", "reset"].includes(el.type);
    const name = clean(el.getAttribute("aria-label") || (isButtonInput ? el.value : "") ||
                       (tag === "a" || tag === "button" ? el.innerText : ""));
    let label = "";
    if (el.labels && el.labels.length) label = strip(el.labels[0].innerText);
    else if (["input", "select", "textarea"].includes(tag) && !isButtonInput) {
      const cell = el.closest("td");
      if (cell && cell.previousElementSibling) label = strip(cell.previousElementSibling.innerText);
    }
    return { role, name: name.slice(0, 80), label, field_name: el.getAttribute("name") || "",
             sensitive: tag === "input" && el.type === "password" };
  }

  document.addEventListener("click", (e) => {
    const el = e.target.closest && e.target.closest("a, button, input, select, textarea, [role], [onclick]");
    if (el && !(el.tagName === "INPUT" && !["submit", "button", "reset", "checkbox", "radio"].includes(el.type))) {
      report({ kind: "click", ...describe(el) });
    }
  }, true);

  const isField = (el) => el && ["INPUT", "SELECT", "TEXTAREA"].includes(el.tagName);
  const reportField = (el) => {
    const d = describe(el);
    const value = d.sensitive ? null
      : el.tagName === "SELECT" ? clean((el.options[el.selectedIndex] || {}).text) : el.value;
    el.__cuaReported = el.value;
    report({ kind: el.tagName === "SELECT" ? "select" : "fill", value, ...d });
  };

  document.addEventListener("change", (e) => { if (isField(e.target)) reportField(e.target); }, true);

  // `change` only fires when a field loses focus. Before control is handed back, report the field
  // the human is still in if its value changed, so the last edit is not lost.
  window.__cuaFlush = () => {
    const el = document.activeElement;
    if (isField(el) && el.value !== (el.__cuaReported ?? el.defaultValue)) reportField(el);
  };
})();
