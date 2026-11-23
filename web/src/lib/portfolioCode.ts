/** The portfolio's short code for the agents (`portfolios.code`): 2–6 uppercase letters or
 * digits, unique among the portfolios that are not archived; empty means none. */
export const CODE_RE = /^[A-Z0-9]{2,6}$/;
export const CODE_ERROR = "Da 2 a 6 lettere maiuscole o cifre";
export const CODE_HINT = "2–6 lettere o cifre, usato dagli agenti nei titoli dei task; vuoto: nessuno";
