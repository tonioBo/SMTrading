// Applies the saved light theme before the first paint (a file, not inline: the CSP
// allows only same-origin scripts).
try {
  if (localStorage.getItem("smt-theme") === "light") document.documentElement.classList.remove("dark");
} catch (e) {
  // storage unavailable: keep the dark default
}
