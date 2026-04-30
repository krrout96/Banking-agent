import React from 'react';
import ReactMarkdown from 'react-markdown';
import { Bot, User, Download } from 'lucide-react';

// Custom Image Renderer with Download Button
const ImageRenderer = ({ src, alt }) => {
    const handleDownload = async () => {
        try {
            const response = await fetch(src);
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `architecture-diagram-${Date.now()}.png`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch {
            // Fallback: open in new tab
            window.open(src, '_blank');
        }
    };

    return (
        <div className="diagram-container">
            <img src={src} alt={alt || 'Architecture Diagram'} className="diagram-image" />
            <button
                onClick={handleDownload}
                className="download-btn"
                title="Download diagram"
            >
                <Download size={16} /> Download
            </button>
        </div>
    );
};

const MessageBubble = ({ role, content }) => {
    const isAgent = role === 'assistant' || role === 'model';

    return (
        <div className={`message-row ${isAgent ? 'agent' : 'user'}`}>
            <div className="avatar">
                {isAgent ? <Bot size={20} /> : <User size={20} />}
            </div>

            <div className="bubble glass-card">
                {isAgent ? (
                    <div className="markdown-content">
                        <ReactMarkdown components={{ img: ImageRenderer }}>
                            {content}
                        </ReactMarkdown>
                    </div>
                ) : (
                    <p>{content}</p>
                )}
            </div>

            <style jsx="true">{`
        .message-row {
          display: flex;
          gap: 12px;
          margin-bottom: 24px;
          max-width: 800px;
          width: 100%;
          animation: fadeIn 0.3s ease-out;
        }
        
        .message-row.user {
          flex-direction: row-reverse;
          margin-left: auto;
        }
        
        .avatar {
          width: 36px;
          height: 36px;
          border-radius: 50%;
          display: flex;
          align-items: center;
          justify-content: center;
          flex-shrink: 0;
          background: ${isAgent ? 'var(--accent-gradient)' : 'var(--bg-tertiary)'};
          color: white;
        }

        .bubble {
          padding: 16px 20px;
          border-radius: var(--radius-md);
          border-top-left-radius: ${isAgent ? '2px' : 'var(--radius-md)'};
          border-top-right-radius: ${isAgent ? 'var(--radius-md)' : '2px'};
          color: var(--text-primary);
          line-height: 1.6;
          font-size: 0.95rem;
          min-width: 0; /* Prevents overflow */
        }

        /* Markdown Styles */
        .markdown-content :global(p) { margin-bottom: 0.8em; }
        .markdown-content :global(p:last-child) { margin-bottom: 0; }
        .markdown-content :global(code) {
          background: rgba(0,0,0,0.3);
          padding: 2px 6px;
          border-radius: 4px;
          font-family: monospace;
          font-size: 0.9em;
          color: #e2e8f0;
        }
        .markdown-content :global(pre code) {
          background: transparent;
          padding: 0;
          color: inherit;
        }
        .markdown-content :global(pre) {
          background: #0d0d10;
          padding: 12px;
          border-radius: 8px;
          overflow-x: auto;
          margin: 10px 0;
          border: 1px solid var(--glass-border);
        }
        .markdown-content :global(ul), .markdown-content :global(ol) {
          margin-left: 20px;
          margin-bottom: 1em;
        }
        .markdown-content :global(h3) {
          margin-top: 1em;
          margin-bottom: 0.5em;
          color: var(--text-primary);
          font-weight: 600;
        }
        .markdown-content :global(img) {
          max-width: 100%;
          border-radius: 8px;
          border: 1px solid var(--glass-border);
          margin-top: 12px;
          box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        .diagram-container {
          position: relative;
          margin: 16px 0;
          border-radius: 12px;
          overflow: hidden;
          background: var(--bg-secondary);
          padding: 12px;
        }
        .diagram-image {
          width: 100%;
          max-width: 800px;
          height: auto;
          border-radius: 8px;
          display: block;
          margin: 0 auto;
        }
        .download-btn {
          display: inline-flex;
          align-items: center;
          gap: 6px;
          margin-top: 12px;
          padding: 8px 16px;
          background: var(--accent-gradient);
          color: white;
          border: none;
          border-radius: 6px;
          text-decoration: none;
          font-size: 0.9rem;
          font-weight: 500;
          transition: all 0.2s;
          cursor: pointer;
        }
        .download-btn:hover {
          transform: translateY(-2px);
          box-shadow: 0 4px 12px rgba(99, 102, 241, 0.4);
        }
      `}</style>
        </div>
    );
};

export default MessageBubble;
