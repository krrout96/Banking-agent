import React from 'react';

const TypingIndicator = () => {
    return (
        <div className="typing-indicator">
            <span></span>
            <span></span>
            <span></span>

            <style jsx="true">{`
        .typing-indicator {
          display: flex;
          gap: 4px;
          padding: 12px 16px;
          background: var(--bg-tertiary);
          border-radius: var(--radius-md);
          border-top-left-radius: 2px;
          width: fit-content;
          margin-bottom: 24px;
          margin-left: 50px; /* Offset for avatar alignment */
          animation: fadeIn 0.3s;
        }
        
        .typing-indicator span {
          width: 8px;
          height: 8px;
          background: var(--text-secondary);
          border-radius: 50%;
          animation: bounce 1.4s infinite ease-in-out both;
        }
        
        .typing-indicator span:nth-child(1) { animation-delay: -0.32s; }
        .typing-indicator span:nth-child(2) { animation-delay: -0.16s; }
        
        @keyframes bounce {
          0%, 80%, 100% { transform: scale(0); }
          40% { transform: scale(1); }
        }
      `}</style>
        </div>
    );
};

export default TypingIndicator;
