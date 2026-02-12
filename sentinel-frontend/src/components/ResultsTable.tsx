type Props = {
  results: any[];
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
  if (!results || results.length === 0) {
    return (
      <div
        style={{
          marginTop: 24,
          padding: 16,
          background: "#f9f9fb",
          border: "1px solid #eef0f6",
          borderRadius: 6,
        }}
      >
        <h3 style={{ margin: 0 }}>📊 Results</h3>
        <p style={{ color: "#666", marginTop: 8 }}>No results found for this query.</p>
      </div>
    );
  }

  // derive columns from first row but ensure deterministic ordering
  const baseColumns = Object.keys(results[0]);

  // common column priorities for enterprise UX
  const commonPriority = ["id", "name", "email", "user_id", "created_at", "updated_at"];

  const orderKey = (col: string) => {
    const prefIndex = preferredOrder.indexOf(col);
    if (prefIndex !== -1) return prefIndex - 1000; // keep preferred first

    const commonIndex = commonPriority.indexOf(col);
    if (commonIndex !== -1) return commonIndex - 500;

    return 0; // default: sort alphabetically after priorities
  };

  const columns = baseColumns.slice().sort((a, b) => {
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
      <h3>📊 Results</h3>

      <div style={{ overflowX: "auto", borderRadius: 8, border: "1px solid #e0e0e0" }}>
        <table
          style={{
            borderCollapse: "collapse",
            width: "100%",
            minWidth: 600,
            fontSize: 14,
          }}
        >
          <thead>
            <tr style={{ background: "#2d3748", color: "white" }}>
              {columns.map((col) => (
                <th
                  key={col}
                  style={{
                    textAlign: "left",
                    padding: 12,
                    fontWeight: 600,
                    borderBottom: "2px solid #1a202c",
                  }}
                >
                  {col}
                </th>
              ))}
            </tr>
          </thead>

          <tbody>
            {results.map((row, idx) => (
              <tr
                key={idx}
                style={{
                  background: idx % 2 === 0 ? "#f9fafb" : "#ffffff",
                  transition: "background-color 0.2s",
                }}
                onMouseOver={(e) => (e.currentTarget.style.background = idx % 2 === 0 ? "#edf2f7" : "#f0f4f8")}
                onMouseOut={(e) => (e.currentTarget.style.background = idx % 2 === 0 ? "#f9fafb" : "#ffffff")}
              >
                {columns.map((col) => (
                  <td
                    key={col}
                    style={{
                      padding: 12,
                      borderBottom: "1px solid #e0e0e0",
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
