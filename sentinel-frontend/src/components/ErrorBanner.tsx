type Props = {
  message: string;
};

export default function ErrorBanner({ message }: Props) {
  return (
    <div className="notice notice-bad" role="alert">
      <span className="dot dot-bad" aria-hidden="true" />
      <span>{message}</span>
    </div>
  );
}
