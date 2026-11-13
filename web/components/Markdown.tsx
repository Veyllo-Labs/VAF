// SPDX-FileCopyrightText: 2026 Veyllo GmbH
// SPDX-License-Identifier: AGPL-3.0-or-later
// Additional permissions and terms under AGPL Section 7: see LICENSING.md
/**
 * The one react-markdown renderer of the UI (chat bubbles, the Markdown preview of the Code
 * Viewer, the model card preview). It applies the image rule of `web/lib/markdownImages.ts`: an
 * image loads only from this server's file lane, any other image is shown as a link marked with
 * an image icon, whose full address shows on hover and which says to a screen reader what it is.
 */
import React, { useMemo } from 'react';
import { useTranslations } from 'next-intl';
import ReactMarkdown, { type Components, type ExtraProps } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { Image as ImageIcon } from 'lucide-react';
import { rehypeOwnImagesOnly, splitFirstGrapheme } from '@/lib/markdownImages';

type AnchorProps = React.ComponentProps<'a'> & ExtraProps;
type ImgProps = React.ComponentProps<'img'> & ExtraProps;

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeOwnImagesOnly];

// Underlined because the base styles take a link's look away; the colour is the text's own, so
// it reads in both themes. Callers with their own `a` (chat, Code Viewer) keep theirs.
function DefaultAnchor({ node: _node, ...rest }: AnchorProps) {
    return <a className="underline break-all" target="_blank" rel="noopener noreferrer" {...rest} />;
}

/** The label's first character and the rest (`splitFirstGrapheme`), when the label is plain text. */
function splitFirstCharacter(children: React.ReactNode): [string, string] | null {
    const text = typeof children === 'string' ? children
        : Array.isArray(children) && children.length === 1 && typeof children[0] === 'string' ? children[0]
            : null;
    return text ? splitFirstGrapheme(text) : null;
}

function DefaultImage({ node: _node, alt, ...rest }: ImgProps) {
    return <img className="max-w-full h-auto rounded my-2" loading="lazy" alt={alt ?? ''} {...rest} />;
}

export function Markdown({ children, components, className }: {
    children: string;
    components?: Components;
    className?: string;
}) {
    const tc = useTranslations('common');
    const pictureLink = tc('markdownPictureLink');
    const merged = useMemo<Components>(() => {
        const Anchor = (components?.a ?? DefaultAnchor) as React.ComponentType<AnchorProps>;
        // A link that stands for a picture shows the picture icon, says what it is on hover and to
        // a screen reader, and shows its address on hover. A plain inline span: the link's
        // underline does not reach into an inline-flex box, and a long address has to wrap like
        // text, also where the link itself does not break words.
        const ImageAwareAnchor = (props: AnchorProps) => {
            if (!props.node?.properties?.dataImageLink) return <Anchor {...props} />;
            const icon = <ImageIcon size="1em" aria-hidden className="inline-block align-[-0.125em] mr-1" />;
            const split = splitFirstCharacter(props.children);
            return (
                <Anchor {...props}>
                    <span title={`${pictureLink}\n${props.href ?? ''}`} className="[overflow-wrap:anywhere]">
                        {split
                            ? <><span className="whitespace-nowrap">{icon}{split[0]}</span>{split[1]}</>
                            : <>{icon}{props.children}</>}
                        <span className="sr-only"> {pictureLink}</span>
                    </span>
                </Anchor>
            );
        };
        return { img: DefaultImage, ...components, a: ImageAwareAnchor };
    }, [components, pictureLink]);
    const body = (
        <ReactMarkdown remarkPlugins={remarkPlugins} rehypePlugins={rehypePlugins} components={merged}>
            {children}
        </ReactMarkdown>
    );
    return className ? <div className={className}>{body}</div> : body;
}

export default Markdown;
