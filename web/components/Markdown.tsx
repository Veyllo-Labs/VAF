// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * The one react-markdown renderer of the UI (chat bubbles, the Markdown preview of the Code
 * Viewer, the model card preview). It applies the image rule of `web/lib/markdownImages.ts`: an
 * image loads only from this server's file lane, any other image is shown as a link marked with
 * an image icon, whose full address shows on hover.
 */
import React, { useMemo } from 'react';
import ReactMarkdown, { type Components, type ExtraProps } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Image as ImageIcon } from 'lucide-react';
import { rehypeOwnImagesOnly } from '@/lib/markdownImages';

type AnchorProps = React.ComponentProps<'a'> & ExtraProps;
type ImgProps = React.ComponentProps<'img'> & ExtraProps;

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeOwnImagesOnly];

function DefaultAnchor({ node: _node, ...rest }: AnchorProps) {
    return <a target="_blank" rel="noopener noreferrer" {...rest} />;
}

function DefaultImage({ node: _node, alt, ...rest }: ImgProps) {
    return <img className="max-w-full h-auto rounded my-2" loading="lazy" alt={alt ?? ''} {...rest} />;
}

export function Markdown({ children, components, className }: {
    children: string;
    components?: Components;
    className?: string;
}) {
    const merged = useMemo<Components>(() => {
        const Anchor = (components?.a ?? DefaultAnchor) as React.ComponentType<AnchorProps>;
        // A link that stands for a picture shows the picture icon, and its address on hover.
        // A plain inline span: the link's underline does not reach into an inline-flex box, and
        // a long address has to wrap like text.
        const ImageAwareAnchor = (props: AnchorProps) => {
            if (!props.node?.properties?.dataImageLink) return <Anchor {...props} />;
            return (
                <Anchor {...props}>
                    <span title={props.href}>
                        <ImageIcon size="1em" aria-hidden className="inline-block align-[-0.125em] mr-1" />
                        {props.children}
                    </span>
                </Anchor>
            );
        };
        return { img: DefaultImage, ...components, a: ImageAwareAnchor };
    }, [components]);
    const body = (
        <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins} components={merged}>
            {children}
        </ReactMarkdown>
    );
    return className ? <div className={className}>{body}</div> : body;
}

export default Markdown;
