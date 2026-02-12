# SentinelSQL Frontend

Enterprise-grade human-in-the-loop interface for SentinelSQL, an agentic NL → SQL system built with LangGraph.

## Overview

This frontend provides a deterministic, approval-driven workflow for translating natural language queries into SQL. It prioritizes safety, auditability, and enterprise compliance.

## Key Features

- **Explicit Workflow States**: Clear UI states that mirror backend execution lifecycle
  - `idle` → `loading` → `requires_human` | `completed` | `rejected`
  
- **Human-in-the-Loop Approval**: Users can approve, edit, or reject generated SQL before execution
  
- **Run ID Auditing**: Every risky query receives a unique `run_id` for compliance and traceability
  
- **No Silent Failures**: Enterprise error handling with clear, actionable messages
  
- **Deterministic Execution**: No frontend business logic—all decisions sent back to backend for validation
  
- **Professional UX**: Enterprise-grade layout without CSS frameworks (pure CSS-in-JS)

## Architecture

```
React (Vite) ↔ FastAPI ↔ LangGraph ↔ SQLite (read-only)
```

### Component Structure

```
src/
├─ api/              # API client (sentinel.ts)
├─ components/       # Reusable UI components
│  ├─ QueryInput.tsx
│  ├─ SQLReview.tsx  (with run_id display)
│  ├─ ResultsTable.tsx
│  ├─ ErrorBanner.tsx
│  └─ LoadingIndicator.tsx
├─ types/            # TypeScript interfaces
│  └─ api.ts         (SentinelResponse, ApprovalDecision)
├─ App.tsx           # Main app with workflow logic
└─ index.css         # Global styles
```

## Workflow

1. User submits natural language query
2. Frontend sends to backend (`POST /query`)
3. Backend processes, returns status:
   - **`requires_human`**: Show approval UI with generated SQL
   - **`completed`**: Show results
   - **`rejected`**: Show error message
4. User approves, edits, or rejects
5. Decision sent back to backend with `run_id`
6. Backend re-executes or stores audit trail

## API Contracts

### Query Submission

```typescript
POST /query
{
  query: string
}

Response:
{
  status: "requires_human" | "completed" | "rejected"
  run_id: string
  sql: string
  reason?: string  // Why human approval is needed
  results?: any[]   // Only if status === "completed"
}
```

### Decision Submission

```typescript
POST /decision
{
  run_id: string
  decision: "approve" | "edit" | "reject"
  edited_sql?: string  // Only if decision === "edit"
}
```

## Development

### Install Dependencies

```bash
npm install
```

### Run Development Server

```bash
npm run dev
```

Starts Vite dev server at `http://localhost:5173`

### Build for Production

```bash
npm run build
```

### Type Checking

```bash
npm run type-check
```

## Enterprise Considerations

### Auditability
- Every approval decision includes a unique `run_id`
- Run ID can be logged to compliance systems
- Audit trail includes: user decision, timestamp, SQL executed

### No Silent Failures
- All errors displayed in red banners with clear messages
- Never shows raw stack traces
- User always knows why something failed

### Safety By Default
- All generated SQL requires human review
- Disabled buttons during processing (no double submits)
- Clear loading states

### Deterministic Execution
- No client-side SQL manipulation
- All logic on backend (LangGraph)
- Frontend is read-only except for approval/edit/reject

## Technologies

- **React 18** with TypeScript
- **Vite** for fast bundling
- **CSS-in-JS** (inline styles) for zero CSS bloat
- **Fetch API** for HTTP requests

## Future Enhancements

- Run history view (past approvals)
- Batch query approval
- Custom SQL validation rules
- Approval workflows (multi-level sign-off)
- Query templates

---

**Built with enterprise compliance and developer clarity in mind.**

```js
export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...

      // Remove tseslint.configs.recommended and replace with this
      tseslint.configs.recommendedTypeChecked,
      // Alternatively, use this for stricter rules
      tseslint.configs.strictTypeChecked,
      // Optionally, add this for stylistic rules
      tseslint.configs.stylisticTypeChecked,

      // Other configs...
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```

You can also install [eslint-plugin-react-x](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-x) and [eslint-plugin-react-dom](https://github.com/Rel1cx/eslint-react/tree/main/packages/plugins/eslint-plugin-react-dom) for React-specific lint rules:

```js
// eslint.config.js
import reactX from 'eslint-plugin-react-x'
import reactDom from 'eslint-plugin-react-dom'

export default defineConfig([
  globalIgnores(['dist']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      // Other configs...
      // Enable lint rules for React
      reactX.configs['recommended-typescript'],
      // Enable lint rules for React DOM
      reactDom.configs.recommended,
    ],
    languageOptions: {
      parserOptions: {
        project: ['./tsconfig.node.json', './tsconfig.app.json'],
        tsconfigRootDir: import.meta.dirname,
      },
      // other options...
    },
  },
])
```
