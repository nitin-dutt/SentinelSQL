import { useState, useCallback, useRef, useLayoutEffect, type KeyboardEvent, type FC } from 'react';

interface QueryInputProps {
  onSubmit: (query: string) => void;
  loading: boolean;
}

const QueryInput: FC<QueryInputProps> = ({ onSubmit, loading }) => {
  const [query, setQuery] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = 'auto';
      textarea.style.height = `${textarea.scrollHeight}px`;
    }
  }, [query]);

  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      if (query.trim() && !loading) {
        onSubmit(query);
        setQuery('');
      }
    }
  }, [query, loading, onSubmit]);

  const handleSubmit = useCallback(() => {
    if (query.trim() && !loading) {
      onSubmit(query);
      setQuery('');
    }
  }, [query, loading, onSubmit]);

  const styles = {
    container: {
      display: 'flex',
      alignItems: 'center',
      gap: '12px',
      padding: '12px 12px 12px 20px',
      backgroundColor: 'rgba(255, 255, 255, 0.05)',
      borderRadius: '24px',
      border: '1px solid var(--border)',
      boxShadow: '0 4px 12px rgba(0,0,0,0.1)',
    },
    textarea: {
      width: '100%',
      flexGrow: 1,
      border: 'none',
      backgroundColor: 'transparent',
      fontFamily: 'inherit',
      fontSize: '1rem',
      lineHeight: '1.5',
      color: 'var(--text-main)',
      resize: 'none' as const,
      outline: 'none',
      padding: '4px 0',
      maxHeight: '200px',
      overflowY: 'auto' as const,
    },
    button: {
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      border: 'none',
      backgroundColor: 'var(--accent)',
      color: 'white',
      width: '40px',
      height: '40px',
      borderRadius: '50%',
      cursor: 'pointer',
      flexShrink: 0,
      transition: 'all 0.2s',
    },
    buttonDisabled: {
      backgroundColor: 'rgba(255, 255, 255, 0.1)',
      color: 'var(--text-muted)',
      cursor: 'not-allowed',
    },
    spinner: {
      border: '2px solid rgba(255, 255, 255, 0.3)',
      borderTop: '2px solid currentColor',
      borderRadius: '50%',
      width: '18px',
      height: '18px',
      animation: 'spin 1s linear infinite',
    },
    sendIcon: {
      width: '20px',
      height: '20px',
    }
  };

  return (
    <div style={styles.container}>
      <textarea
        ref={textareaRef}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="Ask a question about your data..."
        disabled={loading}
        style={styles.textarea}
        rows={1}
      />
      <button onClick={handleSubmit} disabled={loading || !query.trim()} style={{ ...styles.button, ...((loading || !query.trim()) ? styles.buttonDisabled : {}) }} aria-label="Run query">
        {loading ? <div style={styles.spinner}></div> : <span style={{ fontSize: '18px', lineHeight: 1 }}>➤</span>}
      </button>
    </div>
  );
};

export default QueryInput;