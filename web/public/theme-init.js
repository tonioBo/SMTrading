// Applies the saved theme before the first paint (a file, not inline: the CSP allows
// only same-origin scripts). Same rules as src/theme.ts: dark by default, "light",
// "auto" = prefers-color-scheme.
try {
  var t = localStorage.getItem("smt-theme");
  var light = t === "light" || (t === "auto" && window.matchMedia && !window.matchMedia("(prefers-color-scheme: dark)").matches);
  if (light) document.documentElement.classList.remove("dark");
} catch (e) {
  // storage unavailable: keep the dark default
}
