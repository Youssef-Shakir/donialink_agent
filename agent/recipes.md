# Recipes, food cost and stock

This database runs the DoniaLink **Recipe & Food Costing** module (`pos_recipe_costing`).

## How it works

- A **recipe** (`restaurant.recipe`) lists the ingredients and quantities of one menu
  item, for a number of **portions**. Cost per portion = sum of ingredient costs / portions.
- Each recipe is linked to a **kit** (bill of materials of type kit). When the cashier sells
  a menu item, Odoo doesn't move the menu item. It moves its **ingredients** out of stock.
  That is how stock follows sales automatically.
- **Sub-recipes** (type *component*: sauces, doughs, syrups...) are used as ingredients
  of other recipes. They have no selling price.
- **Ingredient cost** = the product's current cost (average purchase price). Each cost
  change is recorded (`ingredient_price_changes`) and updates recipe costs.
- **Food cost %** = ingredient cost / selling price (per recipe) or ingredient cost
  consumed / net sales without tax (for a period, `food_cost_overview`). For cafés and
  restaurants, 25–35 % is usual. Shisha (أراكيل) and drinks are usually much lower.

## Theoretical vs real

- `ingredient_consumption` and `food_cost_overview` give the **theoretical** usage:
  what the recipes say was used for what was sold.
- A **stocktake** (جرد) compares the stock the system expects with a physical count.
  *Variance = counted − system*. A negative variance means a **shortage**: unrecorded
  waste, theft, portions bigger than the recipe, missing purchase receipts or wrong
  recipes. A positive variance means a surplus: often purchases or opening stock that
  were not entered, or recipes that are too heavy.
- **Waste** (هدر) records losses that staff declared, with a reason.
- Real food cost ≈ theoretical food cost + waste + stocktake shortages.

## Reading the numbers

- The first stocktake after go-live often shows a large surplus because opening stock
  was incomplete. This is normal and not a sign of a problem.
- **Negative stock** in `stock_levels` means sales consumed more than was received:
  check missing purchase receipts first.
- **Menu items without a recipe** (`products_without_recipe`) consume nothing, so they
  make food cost look too good. Items tracked in stock themselves (bottled water,
  cans) are fine without a recipe.
- Amounts are in the company currency (usually IQD). Quantities are in each
  ingredient's unit (`unit`).
