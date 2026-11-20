import { useState, useCallback, useRef, useLayoutEffect, type KeyboardEvent, type FC } from 'react';
import Icon from './Icon';

interface QueryInputProps {
  onSubmit: (query: string) => void;
  loading: boolean;
  /** When set (public demo), shows a live counter and refuses to send longer questions. */
  maxChars?: number;
}

const QueryInput: FC<QueryInputProps> = ({ onSubmit, loading, maxChars }) => {
  const [query, setQuery] = useState('');
  const tooLong = maxChars !== undefined && query.length > maxChars;
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
      if (query.trim() && !loading && !tooLong) {
        onSubmit(query);
        setQuery('');
      }
    }
  }, [query, loading, onSubmit, tooLong]);

  const handleSubmit = useCallback(() => {
    if (query.trim() && !loading && !tooLong) {
      onSubmit(query);
      setQuery('');
    }
  }, [query, loading, onSubmit, tooLong]);

  return (
    <div>
      <div className={tooLong ? 'ask ask-bad' : 'ask'}>
        <textarea
          ref={textareaRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Ask a question about your data"
          aria-label="Question"
          disabled={loading}
          rows={1}
        />
        <button className="btn btn-primary ask-send" onClick={handleSubmit} disabled={loading || !query.trim() || tooLong} aria-label="Run query">
          {loading ? <span className="spinner" /> : <Icon name="arrow-up" size={18} />}
        </button>
      </div>
      {maxChars !== undefined && (
        <div className={tooLong ? 'meta ask-count text-bad' : 'meta ask-count'}>
          {tooLong ? `Too long: shorten the question by ${query.length - maxChars} characters. ` : ''}{query.length} / {maxChars}
        </div>
      )}
    </div>
  );
};

export default QueryInput;
