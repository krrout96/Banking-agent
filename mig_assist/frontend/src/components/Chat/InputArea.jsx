import React, { useState, useRef } from 'react';
import { Send, Image as ImageIcon, X } from 'lucide-react';

const InputArea = ({ onSend, isDisabled }) => {
    const [input, setInput] = useState('');
    const [selectedImage, setSelectedImage] = useState(null); // base64 string
    const fileInputRef = useRef(null);

    const handleSend = () => {
        if ((!input.trim() && !selectedImage) || isDisabled) return;
        onSend(input, selectedImage);
        setInput('');
        setSelectedImage(null);
    };

    const handleKeyDown = (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
    };

    const handleFileChange = (e) => {
        const file = e.target.files[0];
        if (file) {
            const reader = new FileReader();
            reader.onloadend = () => {
                // Remove data URL prefix (data:image/png;base64,) to send pure base64
                const rawBase64 = reader.result.split(',')[1];
                setSelectedImage({
                    preview: reader.result, // For display
                    payload: rawBase64      // For API
                });
            };
            reader.readAsDataURL(file);
        }
    };

    return (
        <div className="input-container glass-card">
            {/* Image Preview */}
            {selectedImage && (
                <div className="image-preview">
                    <img src={selectedImage.preview} alt="Upload preview" />
                    <button className="remove-btn" onClick={() => setSelectedImage(null)}>
                        <X size={14} />
                    </button>
                </div>
            )}

            <div className="controls">
                <button
                    className="icon-btn"
                    onClick={() => fileInputRef.current?.click()}
                    title="Upload Architecture Diagram"
                >
                    <ImageIcon size={20} />
                </button>
                <input
                    type="file"
                    ref={fileInputRef}
                    style={{ display: 'none' }}
                    accept="image/*"
                    onChange={handleFileChange}
                />

                <textarea
                    value={input}
                    onChange={(e) => setInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder="Ask about migration or upload a diagram..."
                    rows={1}
                    disabled={isDisabled}
                />

                <button
                    className="send-btn"
                    onClick={handleSend}
                    disabled={(!input.trim() && !selectedImage) || isDisabled}
                >
                    <Send size={20} />
                </button>
            </div>

            <style jsx="true">{`
        .input-container {
          padding: 12px;
          border-radius: var(--radius-lg);
          margin: 0 auto;
          width: 100%;
          max-width: 800px;
          position: relative;
        }

        .image-preview {
          position: absolute;
          top: -70px; /* Pop above the input */
          left: 10px;
          width: 60px;
          height: 60px;
          border-radius: 8px;
          overflow: hidden;
          border: 1px solid var(--glass-border);
          box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        }
        
        .image-preview img {
          width: 100%;
          height: 100%;
          object-fit: cover;
        }

        .remove-btn {
          position: absolute;
          top: 2px;
          right: 2px;
          background: rgba(0,0,0,0.6);
          border: none;
          color: white;
          border-radius: 50%;
          width: 16px;
          height: 16px;
          display: flex;
          align-items: center;
          justify-content: center;
          cursor: pointer;
        }

        .controls {
          display: flex;
          align-items: flex-end; /* Align bottom for textarea growth */
          gap: 12px;
        }

        .icon-btn {
          background: transparent;
          border: none;
          color: var(--text-secondary);
          cursor: pointer;
          padding: 8px;
          border-radius: 50%;
          transition: 0.2s;
        }
        .icon-btn:hover {
          background: var(--bg-tertiary);
          color: var(--text-primary);
        }

        textarea {
          flex: 1;
          background: transparent;
          border: none;
          color: var(--text-primary);
          font-size: 1rem;
          resize: none;
          max-height: 120px;
          padding: 8px 0;
          font-family: inherit;
        }
        textarea:focus {
          outline: none;
        }
        textarea::placeholder {
          color: rgba(255,255,255,0.3);
        }

        .send-btn {
          background: var(--accent-gradient);
          border: none;
          color: white;
          width: 40px;
          height: 40px;
          border-radius: 12px;
          display: flex;
          align-items: center;
          justify-content: center;
          cursor: pointer;
          transition: opacity 0.2s;
        }
        .send-btn:disabled {
          opacity: 0.5;
          cursor: not-allowed;
        }
      `}</style>
        </div>
    );
};

export default InputArea;
