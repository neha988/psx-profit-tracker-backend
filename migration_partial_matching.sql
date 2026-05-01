-- Migration: Add partial matching support
-- Run this in Supabase SQL Editor to enable partial trade matching

-- Add matched_quantity column to track how much of a trade has been matched
ALTER TABLE public.trades 
ADD COLUMN matched_quantity integer DEFAULT 0;

-- Update existing matched trades to have matched_quantity equal to quantity
UPDATE public.trades 
SET matched_quantity = quantity 
WHERE matched = true;

-- Create index for performance
CREATE INDEX idx_trades_remaining_qty ON public.trades(user_id, symbol, trade_type) 
WHERE matched = false OR matched_quantity < quantity;

-- Add comment explaining the column
COMMENT ON COLUMN public.trades.matched_quantity IS 
'Amount of shares in this trade that have been matched. When matched_quantity < quantity, the trade is partially matched and has remaining quantity';
