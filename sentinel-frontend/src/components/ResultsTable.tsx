type Props = {
  results: any[] | { columns: string[]; rows: any[] };
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
      <div
        style={{
          marginTop: 24,
          padding: 24,
          background: "rgba(255, 255, 255, 0.03)",
          border: "1px solid var(--border)",
          borderRadius: 8,
          textAlign: "center",
        }}
      >
        <h3 style={{ margin: 0, color: "var(--text-main)" }}>📊 Results</h3>
        <p style={{ color: "var(--text-muted)", marginTop: 8 }}>No results found for this query.</p>
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
    return String(value);
  };

  return (
    <div style={{ marginTop: 24 }}>
      <h3 style={{ color: "var(--text-main)", marginBottom: 12 }}>📊 Results</h3>

      <div style={{ overflowX: "auto", borderRadius: 8, border: "1px solid var(--border)" }}>
        <table
          style={{
            borderCollapse: "collapse",
            width: "100%",
            minWidth: 600,
            fontSize: 14,
          }}
        >
          <thead>
            <tr style={{ background: "rgba(15, 23, 42, 0.85)" }}>
              {columns.map((col: string) => (
                <th
                  key={col}
                  style={{
                    textAlign: "left",
                    padding: "12px 16px",
                    fontWeight: 600,
                    borderBottom: "1px solid var(--border)",
                    color: "var(--text-muted)",
                    fontSize: "12px",
                    textTransform: "uppercase",
                    letterSpacing: "0.5px"
                  }}
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {rowsData.map((row: any, idx: number) => (
              <tr
                key={idx}
                style={{
                  background: idx % 2 === 0 ? "rgba(255, 255, 255, 0.02)" : "rgba(255, 255, 255, 0.04)",
                  transition: "background-color 0.15s ease",
                }}
                onMouseOver={(e) => (e.currentTarget.style.background = "rgba(59, 130, 246, 0.12)")}
                onMouseOut={(e) => (e.currentTarget.style.background = idx % 2 === 0 ? "rgba(255, 255, 255, 0.02)" : "rgba(255, 255, 255, 0.04)")}
              >
                {columns.map((col: string) => (
                  <td
                    key={col}
                    style={{
                      padding: "12px 16px",
                      borderBottom: "1px solid var(--border)",
                      color: "var(--text-main)",
                      maxWidth: 300,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                    title={String(row[col])}
                  >
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
