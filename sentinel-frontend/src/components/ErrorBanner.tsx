type Props = {
  message: string;
};

export default function ErrorBanner({ message }: Props) {
  return (
    <div
      style={{
        background: "#fff5f5",
        color: "#c53030",
        padding: 16,
        marginTop: 32,
        marginBottom: 32,
        borderRadius: 8,
        border: "2px solid #fc8181",
        boxShadow: "0 4px 6px rgba(0,0,0,0.07)",
        fontSize: 15,
        lineHeight: 1.5,
      }}
    >
      <strong style={{ display: "block", marginBottom: 4 }}>❌ Error</strong>
      {message}
    </div>
  );
}
