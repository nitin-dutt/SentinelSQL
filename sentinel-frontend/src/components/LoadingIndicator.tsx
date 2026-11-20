export default function LoadingIndicator() {
  return (
    <div className="loading" role="status">
      <span className="spinner" aria-hidden="true" />
      Processing request…
    </div>
  );
}
