export default function LoadingIndicator() {
  return (
    <div
      style={{
        marginTop: 24,
        marginBottom: 24,
        padding: 20,
        background: "#ebf8ff",
        color: "#2c5282",
        border: "2px solid #90cdf4",
        borderRadius: 8,
        fontSize: 15,
        fontWeight: 500,
        display: "flex",
        alignItems: "center",
        gap: 12,
      }}
    >
      <span style={{ fontSize: 18 }}>⏳</span>
      Processing request…
    </div>
  );
}
