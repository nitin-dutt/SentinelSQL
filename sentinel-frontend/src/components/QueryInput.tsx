import React, { useState, useCallback, useRef, useLayoutEffect } from 'react';

interface QueryInputProps {
  onSubmit: (query: string) => void;
  loading: boolean;
}

const QueryInput: React.FC<QueryInputProps> = ({ onSubmit, loading }) => {
  const [query, setQuery] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Auto-resize textarea height based on content
  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (textarea) {
      textarea.style.height = 'auto'; // Reset height
      textarea.style.height = `${textarea.scrollHeight}px`;
    }
  }, [query]);

  const handleKeyDown = useCallback((event: React.KeyboardEvent<HTMLTextAreaElement>) => {
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
      gap: '8px',
      padding: '8px 8px 8px 16px',
      backgroundColor: '#f0f2f5',
      borderRadius: '24px',
      border: '1px solid #dfe1e5',
      boxShadow: '0 1px 2px rgba(0,0,0,0.05)',
    },
    textarea: {
      width: '100%',
      flexGrow: 1,
      border: 'none',
      backgroundColor: 'transparent',
      fontFamily: 'inherit',
      fontSize: '1rem',
      lineHeight: '1.5',
      color: 'black',
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
      backgroundColor: '#1a73e8',
      color: 'white',
      width: '36px',
      height: '36px',
      borderRadius: '50%',
      cursor: 'pointer',
      flexShrink: 0,
      transition: 'background-color 0.2s',
    },
    buttonDisabled: {
      backgroundColor: '#a0c3ff',
      cursor: 'not-allowed',
    },
    spinner: {
      border: '3px solid rgba(255, 255, 255, 0.3)',
      borderTop: '3px solid white',
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
        placeholder="Enter your query"
        disabled={loading}
        style={styles.textarea}
        rows={1}
      />
      <button onClick={handleSubmit} disabled={loading || !query.trim()} style={{ ...styles.button, ...((loading || !query.trim()) ? styles.buttonDisabled : {}) }} aria-label="Run query">
        {loading ? <div style={styles.spinner}></div> : <svg style={styles.sendIcon} xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M2.01 21L23 12 2.01 3 2 10l15 2-15 2z" /></svg>}
      </button>
    </div>
  );
};

export default QueryInput;