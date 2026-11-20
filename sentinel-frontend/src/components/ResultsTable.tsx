type Props = {
  results: any[] | { columns: string[]; rows: any[]; truncated?: boolean; row_limit?: number };
  /** Optional: prioritize these columns first, in order */
  preferredOrder?: string[];
  /** Optional: formatters per column */
  formatters?: Record<string, (value: any) => string>;
};

function detectDate(value: any) {
  if (typeof value !== "string") return false;
  // simple ISO date-ish detection
  return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}/.test(value);
}

export default function ResultsTable({
  results,
  preferredOrder = [],
  formatters = {},
}: Props) {
  const isObjectFormat = results && !Array.isArray(results) && "rows" in results;
  const rowsData = isObjectFormat ? (results as any).rows : results;
  const columnsData = isObjectFormat ? (results as any).columns : null;

  if (!rowsData || rowsData.length === 0) {
    return (
      <div className="field-gap">
        <span className="label">Results</span>
        <div className="notice" style={{ display: "block" }}>
          <p>No rows matched this query</p>
          <p className="muted small" style={{ marginTop: 4 }}>
            If your question is not about the data in this database (see the table list), the system may have guessed a filter that matches nothing. Try rephrasing it in terms of the tables and columns.
          </p>
        </div>
      </div>
    );
  }

  // derive columns from explicit columns data or fallback to first row keys
  const baseColumns: string[] = (columnsData as string[]) || (rowsData[0] ? Object.keys(rowsData[0]) : []);

  // common column priorities for enterprise UX
  const commonPriority = ["id", "name", "email", "user_id", "created_at", "updated_at"];

  const orderKey = (col: string) => {
    const prefIndex = preferredOrder.indexOf(col);
    if (prefIndex !== -1) return prefIndex - 1000; // keep preferred first

    const commonIndex = commonPriority.indexOf(col);
    if (commonIndex !== -1) return commonIndex - 500;

    return 0; // default: sort alphabetically after priorities
  };

  const columns: string[] = baseColumns.slice().sort((a: string, b: string) => {
    const ka = orderKey(a);
    const kb = orderKey(b);
    if (ka !== kb) return ka - kb;
    return a.localeCompare(b);
  });

  const renderCell = (col: string, value: any) => {
    if (formatters[col]) return formatters[col](value);
    if (value === null || value === undefined) return "";
    if (detectDate(value)) {
      try {
        return new Date(value).toLocaleString();
      } catch {
        return String(value);
      }
    }
    if (typeof value === "object") return JSON.stringify(value);
    // Averages and ratios arrive with every decimal the database computed (122666.66666666667): show two. Whole numbers (ids, counts) are left alone.
    if (typeof value === "number" && !Number.isInteger(value)) {
      return value.toLocaleString(undefined, { maximumFractionDigits: 2 });
    }
    return String(value);
  };

  // A column whose every value is a number is set right-aligned in tabular figures, header included, so digits line up.
  const numeric = new Set(
    columns.filter((col) => {
      const values = rowsData.map((row: any) => row[col]).filter((v: any) => v !== null && v !== undefined);
      return values.length > 0 && values.every((v: any) => typeof v === "number");
    })
  );

  return (
    <div className="field-gap">
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline" }}>
        <span className="label">Results</span>
        <span className="meta">{rowsData.length} {rowsData.length === 1 ? "row" : "rows"}</span>
      </div>
      {isObjectFormat && (results as any).truncated && (
        <p className="muted small" style={{ margin: "4px 0 8px" }}>
          Row limit reached: showing the first {(results as any).row_limit ?? rowsData.length} rows. The full answer may have more.
          Ask for a narrower question (a filter, a count, or a top-N) to see the rest.
        </p>
      )}

      <div className="table-wrap" style={{ borderRadius: 8 }}>
        <table className="table table-hover table-fit">
          <thead>
            <tr>
              {columns.map((col: string) => (
                <th key={col} className={numeric.has(col) ? "num" : "mono"}>
                  {col}
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {rowsData.map((row: any, idx: number) => (
              <tr key={idx}>
                {columns.map((col: string) => (
                  <td key={col} className={numeric.has(col) ? "num" : "truncate"} title={String(row[col])}>
                    {renderCell(col, row[col])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
