'use client';

import React, { useMemo } from 'react';
import {
    Chart as ChartJS,
    CategoryScale,
    LinearScale,
    PointElement,
    LineElement,
    Filler,
    Tooltip,
} from 'chart.js';
import { Line } from 'react-chartjs-2';

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Filler, Tooltip);

export interface Stats4hBucket {
    bucket_ts: number;
    count: number;
}

export interface MessagesChartProps {
    buckets: Stats4hBucket[];
    /** Unique id to avoid canvas/Chart instance conflicts when both dashboards could mount */
    chartId?: string;
}

function formatBucketLabel(bucket_ts: number): string {
    const d = new Date(bucket_ts * 1000);
    const day = d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
    const h = d.getHours();
    return `${day} ${h}h`;
}

export default function MessagesChart({ buckets: rawBuckets, chartId = 'messages-chart' }: MessagesChartProps) {
    const buckets = useMemo(() => {
        if (rawBuckets.length > 0) return rawBuckets;
        const now = Math.floor(Date.now() / 1000);
        const bucketSeconds = 4 * 3600;
        const start = Math.floor((now - 7 * 24 * 3600) / bucketSeconds) * bucketSeconds;
        return Array.from({ length: 42 }, (_, i) => ({
            bucket_ts: start + i * bucketSeconds,
            count: 0,
        }));
    }, [rawBuckets]);

    const chartData = useMemo(() => ({
        labels: buckets.map((b) => formatBucketLabel(b.bucket_ts)),
        datasets: [
            {
                label: 'Messages',
                data: buckets.map((b) => b.count),
                fill: true,
                borderColor: 'rgb(14, 165, 233)',
                backgroundColor: 'rgba(14, 165, 233, 0.15)',
                tension: 0.3,
                pointRadius: 3,
                pointHoverRadius: 8,
                pointBackgroundColor: 'rgb(14, 165, 233)',
            },
        ],
    }), [buckets]);

    const options = useMemo(() => ({
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { display: false },
            tooltip: {
                callbacks: {
                    label: (ctx: { raw?: unknown }) => {
                        const n = typeof ctx.raw === 'number' ? ctx.raw : 0;
                        return `${n} message${n !== 1 ? 's' : ''} in this interval`;
                    },
                },
            },
        },
        scales: {
            x: {
                grid: { display: false },
                ticks: {
                    maxTicksLimit: 5,
                    font: { size: 11 },
                },
            },
            y: {
                beginAtZero: true,
                grid: { color: 'rgba(0,0,0,0.05)' },
                ticks: {
                    stepSize: 1,
                    font: { size: 10 },
                },
            },
        },
    }), []);

    return (
        <div className="rounded-lg border border-gray-200 bg-gray-50/50 overflow-hidden px-4 pb-2 w-full min-w-0">
            <p className="text-sm font-medium text-gray-700 pt-3 pb-2">Messages per 4-hour interval (last 7 days)</p>
            <div className="w-full h-[120px]">
                <Line data={chartData} options={options} id={chartId} />
            </div>
        </div>
    );
}
