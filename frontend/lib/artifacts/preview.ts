export const MAX_TEXT_PREVIEW_BYTES = 5 * 1024 * 1024;
export const MAX_CSV_PREVIEW_ROWS = 200;

export function parseCsvPreview(
  text: string,
  maxRows = MAX_CSV_PREVIEW_ROWS,
): { rows: string[][]; truncated: boolean } {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  let index = 0;
  let truncated = false;

  const pushRow = () => {
    row.push(cell);
    cell = "";
    rows.push(row);
    row = [];
  };

  while (index < text.length) {
    const char = text[index];
    if (quoted) {
      if (char === '"' && text[index + 1] === '"') {
        cell += '"';
        index += 2;
        continue;
      }
      if (char === '"') quoted = false;
      else cell += char;
    } else if (char === '"' && cell.length === 0) {
      quoted = true;
    } else if (char === ",") {
      row.push(cell);
      cell = "";
    } else if (char === "\n") {
      pushRow();
      if (rows.length >= maxRows) {
        truncated = index < text.length - 1;
        break;
      }
    } else if (char !== "\r") {
      cell += char;
    }
    index += 1;
  }
  if (!truncated && (cell.length > 0 || row.length > 0)) pushRow();
  return { rows, truncated };
}
